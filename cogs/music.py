"""Music streaming for Bot_CR (YTMusic search + yt-dlp + ffmpeg).

Joins the author's voice channel and streams the best audio source from YouTube
(queries are searched via YTMusic, URLs are used directly), with a queue, a
``/queue auto`` YTMusic-radio generator, majority vote-skip, per-track loop,
volume control and an interactive now-playing panel that also looks up lyrics
on LRCLIB. The bot auto-disconnects after being idle for a bit.

The playback state machine lives in :class:`MusicPlayer` and is deliberately
voice-independent where possible (``voice`` is injected), so the queue / vote /
loop math is unit-testable without a real Discord voice connection.
"""

import asyncio
import concurrent.futures
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import aiohttp
import discord
from discord.ext import commands

try:
    import yt_dlp
except ImportError:  # pragma: no cover - exercised at deploy time
    yt_dlp = None

try:
    from ytmusicapi import YTMusic
except ImportError:  # pragma: no cover - exercised at deploy time
    YTMusic = None

from cogs._perms import is_bot_admin

LOG = logging.getLogger("bot.music")

USER_AGENT = "Bot_CR/1.0 (robotics-club Discord bot; contact: server staff)"

URL_RE = re.compile(r"^https?://", re.I)

# YouTube's "Sign in to confirm you're not a bot" block on datacenter IPs.
_BOT_BLOCKED = re.compile(r"sign in to confirm you.*not a bot", re.I)
COOKIES_HINT = ("YouTube is bot-flagging this server's network, so it needs "
                "login cookies before it will stream. Add a cookies.txt for "
                "youtube.com and set `YT_COOKIES_FILE` — see README for how.")

# Authenticated-but-frameless: the account serving cookies is too new/trust-less
# for YouTube to hand out stream formats yet.
_NO_FORMATS = re.compile(r"requested format is not available", re.I)
ACCOUNT_HINT = ("YouTube let us in but offered no stream for that video. This "
                "usually means the YouTube account behind the cookies isn't "
                "trusted yet — watch a few videos while logged in as it, then "
                "re-export cookies.txt and restart the bot.")

# ytmusicapi is not thread-safe, and its calls run via asyncio.to_thread.
_YT_MUSIC: "YTMusic | None" = None
_YT_MUSIC_LOCK = threading.Lock()


def _ytmusic() -> "YTMusic":
    global _YT_MUSIC
    if _YT_MUSIC is None:
        _YT_MUSIC = YTMusic()
    return _YT_MUSIC

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


def _header_option(headers: dict) -> str:
    """Render yt-dlp's ``http_headers`` as an ffmpeg ``-headers`` argument.

    ffmpeg wants CRLF-terminated ``Name: value`` pairs in a single argument.
    discord.py shlex-splits ``before_options``, so the surrounding quotes keep
    the pairs together and the embedded newlines reach ffmpeg intact. Passing
    the same headers yt-dlp used avoids googlevideo 403s mid-stream.
    """
    pairs = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    return f'-headers "{pairs}"'

IDLE_LEAVE_SECONDS = 60
IDLE_CHECK_SECONDS = 10


def skip_threshold(listener_count: int) -> int:
    """Votes required to skip: a majority, at least 2 — or 1 when alone."""
    if listener_count <= 1:
        return 1
    return max(2, listener_count // 2 + 1)


def fmt_duration(seconds) -> str:
    if not seconds:
        return "∞"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass
class Track:
    """One queued song. Votes reset when the track starts playing."""

    title: str
    url: str
    webpage_url: str = ""
    video_id: str = ""
    duration: int | None = None
    thumbnail: str = ""
    artist: str = ""
    requester_id: int | None = None
    headers: dict = field(default_factory=dict)
    votes: set = field(default_factory=set)


class MusicPlayer:
    """Per-guild queue + playback state machine (voice injected for tests)."""

    def __init__(self, bot, guild_id, text_channel=None, *, audio_factory=None):
        self.bot = bot
        self.guild_id = guild_id
        self.text_channel = text_channel
        self.voice = None
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop = False
        self.volume = 0.5
        self.now_playing_message = None
        self.now_playing_view = None
        self._audio_factory = audio_factory
        self._watchdog_task: asyncio.Task | None = None
        self._last_active = time.monotonic()

    def _touch(self):
        """Note recent activity so the idle watchdog doesn't disconnect."""
        self._last_active = time.monotonic()

    def start_watchdog(self):
        """Ensure the idle-leave watchdog is running for this voice session."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _watchdog(self):
        """Leave the channel after IDLE_LEAVE_SECONDS with nothing queued/playing.

        Unlike a one-shot sleep task, this also covers joining without a
        playable track (e.g. a failed search) and survives track hand-offs.
        """
        try:
            while True:
                await asyncio.sleep(IDLE_CHECK_SECONDS)
                if self.voice is None or not self.voice.is_connected():
                    return
                if self.voice.is_playing() or self.voice.is_paused():
                    continue  # actively playing (or deliberately paused) — not idle
                if self.queue or self.current is not None:
                    continue  # something is waiting; don't interrupt it
                if time.monotonic() - self._last_active >= IDLE_LEAVE_SECONDS:
                    await self.voice.disconnect()
                    await self._update_panel(
                        stopped="Left the voice channel after being idle.")
                    return
        except asyncio.CancelledError:
            return

    async def _cancel_watchdog(self):
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            self._watchdog_task = None

    # ── queue / playback ────────────────────────────────────────
    async def enqueue(self, track: Track):
        """Add a track; start playback immediately if nothing is playing."""
        self.queue.append(track)
        self._touch()
        if self.voice and self.voice.is_playing():
            return False
        await self.play_next()
        return True

    async def play_next(self):
        """Start the next track — queue head, or loop the current one."""
        if self.voice is None or not self.voice.is_connected() or self.voice.is_playing():
            return
        if self.loop and self.current is not None:
            track = self.current
        elif self.queue:
            track = self.queue.popleft()
        else:
            track = None
        if track is None:
            self.current = None
            self._touch()  # count the idle window from when the last track ended
            return
        self.current = track
        self.current.votes.clear()
        source = self._make_source(track)
        self.voice.play(source, after=self._after_hook)
        await self._update_panel()

    def _make_source(self, track: Track):
        if self._audio_factory is not None:
            return self._audio_factory(track)
        before = FFMPEG_BEFORE
        if track.headers:
            before = f"{before} {_header_option(track.headers)}"
        audio = discord.FFmpegPCMAudio(
            track.url,
            before_options=before,
            options="-vn",
        )
        return discord.PCMVolumeTransformer(audio, volume=self.volume)

    def _after_hook(self, error):
        if error:
            LOG.warning("Playback error: %s", error)
        future = asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)
        try:
            future.result()
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            pass  # bot/loop shutting down mid-track
        except Exception:  # pragma: no cover - defensive
            LOG.exception("after-hook crashed")

    # ── control ─────────────────────────────────────────────────
    async def stop(self, label: str = "⏹️ Stopped."):
        await self._cancel_watchdog()
        self.queue.clear()
        self.current = None
        if self.voice and self.voice.is_playing():
            self.voice.stop()
        if self.voice and self.voice.is_connected():
            await self.voice.disconnect()
        await self._update_panel(stopped=label)

    async def toggle_loop(self):
        self.loop = not self.loop
        await self._update_panel()
        return self.loop

    async def set_volume(self, percent: float):
        self.volume = max(0.05, min(1.0, percent / 100))
        if self.voice and isinstance(getattr(self.voice, "source", None), discord.PCMVolumeTransformer):
            self.voice.source.volume = self.volume
        return self.volume

    def _listeners(self) -> int:
        if self.voice is None or self.voice.channel is None:
            return 0
        return sum(1 for member in self.voice.channel.members if not member.bot)

    async def vote_skip(self, author: discord.Member) -> tuple[bool, int, int]:
        """Request a skip. Returns (skipped_now, needed, votes_now).

        The requester and bot staff skip instantly; otherwise the track skips
        once a majority (>=2, or 1 when alone) of listeners votes.
        """
        if self.current is None:
            return False, 0, 0
        needed = skip_threshold(self._listeners())
        if author.id == self.current.requester_id or is_bot_admin(author):
            await self._skip_now()
            return True, needed, needed
        self.current.votes.add(author.id)
        votes = len(self.current.votes)
        if votes >= needed:
            await self._skip_now()
            return True, needed, votes
        return False, needed, votes

    async def _skip_now(self):
        if self.voice and self.voice.is_playing():
            self.voice.stop()  # play_next runs via the after-hook
        else:
            await self.play_next()

    # ── panel ───────────────────────────────────────────────────
    def embed(self, stopped: str = ""):
        embed = discord.Embed(color=discord.Color.red())
        if stopped:
            embed.title = stopped
            return embed
        track = self.current
        if track is None:
            embed.title = "🎵 Nothing playing."
            embed.description = "Use `/play <song>` to start something."
            return embed
        embed.title = f"🎵 {track.title}"
        embed.url = track.webpage_url or None
        desc = track.artist or ""
        if track.requester_id:
            desc = (desc + "\n" if desc else "") + f"Requested by <@{track.requester_id}>"
        embed.description = desc or None
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        embed.add_field(name="Duration", value=fmt_duration(track.duration), inline=True)
        embed.add_field(name="Loop", value="🔂 On" if self.loop else "Off", inline=True)
        if self.current and self.current.votes:
            votes = len(self.current.votes)
            embed.add_field(name="Skip votes",
                            value=f"{votes}/{skip_threshold(self._listeners())}", inline=True)
        if self.queue:
            lines = "\n".join(
                f"{i + 1}. **{t.title}**" for i, t in list(self.queue)[:6])
            more = f"\n*+{len(self.queue) - 6} more*" if len(self.queue) > 6 else ""
            embed.add_field(name=f"Up next ({len(self.queue)})", value=lines + more, inline=False)
        else:
            embed.add_field(name="Up next", value="—", inline=False)
        return embed

    async def _update_panel(self, stopped: str = ""):
        message = self.now_playing_message
        if message is None:
            return
        view = self.now_playing_view
        embed = self.embed(stopped=stopped)
        try:
            await message.edit(embed=embed, view=view if not stopped else None)
        except discord.NotFound:
            self.now_playing_message = None
            self.now_playing_view = None


class NowPlayingView(discord.ui.View):
    """Interactive panel: pause/resume, vote-skip, loop, stop, lyrics."""

    def __init__(self, cog, player: MusicPlayer):
        super().__init__(timeout=None)
        self.cog = cog
        self.player = player

    async def _reaction(self, interaction: discord.Interaction, feedback: str):
        await interaction.response.edit_message(
            embed=self.player.embed(), view=self.player.now_playing_view or self)

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.secondary, custom_id="music:pause")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        if player.voice is None or player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        if player.voice.is_paused():
            player.voice.resume()
            feedback = "▶️ Resumed"
        elif player.voice.is_playing():
            player.voice.pause()
            feedback = "⏸️ Paused"
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await self._reaction(interaction, feedback)
        await interaction.followup.send(feedback, ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="music:skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        skipped, needed, votes = await player.vote_skip(interaction.user)
        await self._reaction(interaction, "")
        if skipped:
            await interaction.followup.send(
                f"⏭️ Skipped by {interaction.user.mention}.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"⏭️ Skip vote {votes}/{needed}.", ephemeral=True)

    @discord.ui.button(emoji="🔂", style=discord.ButtonStyle.secondary, custom_id="music:loop")
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = await self.player.toggle_loop()
        await self._reaction(interaction, "")
        await interaction.followup.send(
            f"🔂 Loop {'on' if state else 'off'}.", ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="music:stop")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.stop()
        try:
            await interaction.response.edit_message(
                embed=self.player.embed("⏹️ Stopped."), view=None)
        except discord.NotFound:
            await interaction.response.send_message("⏹️ Stopped.", ephemeral=True)

    @discord.ui.button(emoji="🎤", style=discord.ButtonStyle.success, custom_id="music:lyrics")
    async def lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        player = self.player
        if player.current is None:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await self.cog._lyrics_for(
            player.current.title, player.current.artist)
        if result is None:
            await interaction.followup.send(
                "🎤 No lyrics found for the current track.", ephemeral=True)
            return
        lyrics, title, artist = result
        embed = discord.Embed(
            title=f"🎤 {artist} — {title}",
            description=lyrics[:3500],
            color=discord.Color.pink(),
        )
        embed.set_footer(text="via LRCLIB")
        await interaction.followup.send(embed=embed, ephemeral=True)


class Music(commands.Cog):
    """Queue music from YouTube — /play, /queue auto, /skip, /loop and more."""

    def __init__(self, bot):
        self.bot = bot
        self.players: dict[int, MusicPlayer] = {}
        self._http: aiohttp.ClientSession | None = None

    def _player(self, guild_id: int, text_channel=None) -> MusicPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = MusicPlayer(self.bot, guild_id, text_channel)
            self.players[guild_id] = player
        elif text_channel is not None:
            player.text_channel = text_channel
        return player

    def _cleanup(self, guild_id: int):
        player = self.players.pop(guild_id, None)
        if player is not None and player._watchdog_task is not None:
            player._watchdog_task.cancel()

    async def cog_unload(self):
        for player in list(self.players.values()):
            if player.voice and player.voice.is_connected():
                await player.voice.disconnect()
        if self._http is not None and not self._http.closed:
            await self._http.close()

    # ── lookup helpers ──────────────────────────────────────────
    @staticmethod
    def _ytdl_opts() -> dict:
        """Base yt-dlp options (+ cookiefile from ``YT_COOKIES_FILE``).

        Env is read lazily so setting ``YT_COOKIES_FILE`` in ``.env`` works
        without restarting at import time.
        """
        opts = dict(YTDL_OPTS)
        cookies = os.environ.get("YT_COOKIES_FILE")
        if cookies:
            opts["cookiefile"] = cookies
        return opts

    @staticmethod
    def _extract_audio(url: str) -> Track:
        """Blocking yt-dlp stream extraction for a video URL (to_thread)."""
        if yt_dlp is None:
            raise RuntimeError("yt-dlp is not installed")
        if not URL_RE.match(url):
            raise RuntimeError(f"not a resolvable URL: {url!r}")
        try:
            with yt_dlp.YoutubeDL(Music._ytdl_opts()) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            if _BOT_BLOCKED.search(str(exc)):
                raise RuntimeError(COOKIES_HINT) from exc
            if _NO_FORMATS.search(str(exc)):
                raise RuntimeError(ACCOUNT_HINT) from exc
            raise
        if info.get("entries"):
            info = info["entries"][0]
        if not info or not info.get("url"):
            raise RuntimeError("no playable audio source found")
        return Track(
            title=info.get("title") or url,
            url=info["url"],
            webpage_url=info.get("webpage_url") or info.get("original_url") or url,
            video_id=info.get("id") or "",
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail") or "",
            artist=info.get("artist") or info.get("channel") or info.get("uploader") or "",
            headers=info.get("http_headers") or {},
        )

    @staticmethod
    def _failure_hint(exc: Exception) -> str | None:
        """Map known YouTube failure modes to a human message (None → generic).

        Keeps the user-facing hints in one place so both the search and the
        stream-resolution steps surface the same explanations.
        """
        text = str(exc)
        if _BOT_BLOCKED.search(text) or text == COOKIES_HINT:
            return COOKIES_HINT
        if _NO_FORMATS.search(text) or text == ACCOUNT_HINT:
            return ACCOUNT_HINT
        return None

    @staticmethod
    def _find_song_sync(query: str) -> Track:
        """Resolve a query to a Track (blocking — run via to_thread).

        Direct URLs go straight to yt-dlp and come back fully playable.
        Anything else is searched through YTMusic, which is far more reliable
        than yt-dlp's ``ytsearch`` (that is bot-detected); the result is a
        metadata-only skeleton with an empty ``url`` so the caller can announce
        "found" before the slow, often-flagged stream extraction starts.
        """
        if URL_RE.match(query):
            return Music._extract_audio(query)
        if YTMusic is None:
            raise RuntimeError("ytmusicapi is not installed")
        with _YT_MUSIC_LOCK:
            results = _ytmusic().search(query, filter="songs", limit=1)
        if not results:
            raise RuntimeError(f"no YTMusic results for {query!r}")
        result = results[0]
        video_id = result.get("videoId")
        if not video_id:
            raise RuntimeError("YTMusic result has no videoId")
        artists = ", ".join(a.get("name") for a in (result.get("artists") or [])
                            if a.get("name"))
        secs = result.get("duration_seconds")
        thumbs = result.get("thumbnails") or []
        return Track(
            title=result.get("title") or query,
            url="",
            webpage_url=f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id,
            duration=int(secs) if secs else None,
            thumbnail=thumbs[-1].get("url") if thumbs else "",
            artist=artists,
        )

    @staticmethod
    def _resolve_stream_sync(track: Track) -> Track:
        """Fill in the playable stream URL + headers for a found track.

        Called after ``_find_song_sync`` when the track is only metadata; the
        YTMusic metadata is kept and the yt-dlp result supplies what's missing.
        """
        if track.url:
            return track
        resolved = Music._extract_audio(track.webpage_url)
        track.url = resolved.url
        track.headers = resolved.headers
        track.video_id = resolved.video_id or track.video_id
        if not track.thumbnail:
            track.thumbnail = resolved.thumbnail
        if not track.duration:
            track.duration = resolved.duration
        if not track.artist:
            track.artist = resolved.artist
        return track

    @staticmethod
    def _radio_seed_ids(video_id: str, limit: int) -> list[str]:
        """Nearest-neighbour video IDs for a track's YouTube Music radio."""
        if YTMusic is None:
            raise RuntimeError("ytmusicapi is not installed")
        with _YT_MUSIC_LOCK:
            data = _ytmusic().get_watch_playlist(
                videoId=video_id, radio=True, limit=limit)
        seeds: list[str] = []
        for entry in data.get("tracks") or []:
            vid = entry.get("videoId")
            if vid and vid not in seeds:
                seeds.append(vid)
            if len(seeds) >= limit:
                break
        return seeds

    async def _find_song(self, query: str) -> Track:
        return await asyncio.to_thread(self._find_song_sync, query)

    async def _resolve_stream(self, track: Track) -> Track:
        return await asyncio.to_thread(self._resolve_stream_sync, track)

    async def _resolve_tracks(self, video_ids: list[str]) -> list[Track]:
        """Stream-extract several videos in parallel (radio queue generation)."""
        results = await asyncio.gather(
            *(asyncio.to_thread(self._extract_audio,
                                f"https://www.youtube.com/watch?v={vid}")
              for vid in video_ids),
            return_exceptions=True,
        )
        tracks: list[Track] = []
        for vid, res in zip(video_ids, results):
            if isinstance(res, Exception):
                LOG.warning("Auto-queue: failed to resolve %s: %s", vid, res)
                continue
            res.video_id = vid
            tracks.append(res)
        return tracks

    async def _ensure_voice(self, ctx) -> MusicPlayer | None:
        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.send("🎧 Join a voice channel first.")
            return None
        channel = ctx.author.voice.channel
        player = self._player(ctx.guild.id, ctx.channel)
        existing = discord.utils.get(self.bot.voice_clients, guild=ctx.guild)
        if existing is not None and existing.is_connected():
            if existing.channel != channel:
                await ctx.send(f"⚠️ I'm already playing in {existing.channel.mention}.")
                return None
            player.voice = existing
            player.start_watchdog()
            player._touch()
            return player
        try:
            player.voice = await channel.connect()
        except (discord.Forbidden, discord.ClientException) as exc:
            await ctx.send(f"⛔ Couldn't join the channel: {exc}")
            return None
        player.start_watchdog()
        player._touch()
        return player

    async def _send_panel(self, ctx, player: MusicPlayer):
        view = NowPlayingView(self, player)
        player.now_playing_view = view
        player.now_playing_message = await ctx.send(embed=player.embed(), view=view)

    # ── commands ────────────────────────────────────────────────
    @commands.hybrid_command(name="play", description="Play a song (YouTube search or URL).")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def play(self, ctx, *, query: str):
        """Search the top YouTube result, or stream a direct audio URL."""
        # Slash interactions expire after ~3s; joining voice and searching
        # YouTube can easily take longer, so acknowledge before any awaits.
        # On prefix invocations ctx.defer() is a no-op.
        await ctx.defer()
        player = await self._ensure_voice(ctx)
        if player is None:
            return
        found_msg = None
        try:
            track = await self._find_song(query)
        except Exception as exc:
            LOG.warning("Search failed for %r: %s", query, exc)
            hint = Music._failure_hint(exc)
            await ctx.send(f"⚠️ {hint}" if hint
                           else "⚠️ Couldn't find something playable for that query.")
            return
        # Query results are metadata-only at this point — let the user see the
        # match before the slow, flags-prone stream extraction kicks in.
        if not track.url:
            label = f"**{track.title}**"
            if track.artist:
                label += f" — {track.artist}"
            found_msg = await ctx.send(f"🎵 Found {label} — getting the stream…")
            try:
                track = await self._resolve_stream(track)
            except Exception as exc:
                LOG.warning("Stream resolve failed for %r: %s", query, exc)
                if found_msg:
                    await found_msg.delete()
                hint = Music._failure_hint(exc)
                await ctx.send(f"⚠️ {hint}" if hint
                               else "⚠️ Couldn't get a stream for that track.")
                return
        track.requester_id = ctx.author.id
        started = await player.enqueue(track)
        if found_msg:
            await found_msg.delete()
        if started and player.now_playing_message is None:
            await self._send_panel(ctx, player)
        elif not started:
            await ctx.send(f"➕ **{track.title}** added to the queue (#{len(player.queue)}).")

    @commands.hybrid_command(name="pause", description="Pause the current track.")
    @commands.guild_only()
    async def pause(self, ctx):
        player = self._player(ctx.guild.id)
        if not player.voice or not player.voice.is_playing():
            await ctx.send("🎵 Nothing is playing to pause.")
            return
        player.voice.pause()
        await player._update_panel()
        await ctx.send("⏸️ Paused.", delete_after=8)

    @commands.hybrid_command(name="resume", description="Resume the current track.")
    @commands.guild_only()
    async def resume(self, ctx):
        player = self._player(ctx.guild.id)
        if not player.voice or not player.voice.is_paused():
            await ctx.send("🎵 Nothing is paused.")
            return
        player.voice.resume()
        await player._update_panel()
        await ctx.send("▶️ Resumed.", delete_after=8)

    @commands.hybrid_command(name="skip", description="Vote to skip the current track.")
    @commands.guild_only()
    async def skip(self, ctx):
        """Requester/staff skip instantly; everyone else needs a majority vote."""
        player = self._player(ctx.guild.id)
        if player.voice is None or player.current is None:
            await ctx.send("🎵 Nothing is playing to skip.")
            return
        skipped, needed, votes = await player.vote_skip(ctx.author)
        await player._update_panel()
        if skipped:
            await ctx.send("⏭️ Skipped.")
        else:
            await ctx.send(f"⏭️ Skip vote {votes}/{needed}.")

    @commands.hybrid_command(name="stop", description="Stop playback and leave the channel.")
    @commands.guild_only()
    async def stop(self, ctx):
        player = self._player(ctx.guild.id)
        if player.voice is None or not player.voice.is_connected():
            await ctx.send("🎵 I'm not in a voice channel.")
            return
        await player.stop()
        await ctx.send("⏹️ Stopped and left.")

    @commands.hybrid_command(name="loop", description="Toggle repeat for the current track.")
    @commands.guild_only()
    async def loop(self, ctx):
        player = self._player(ctx.guild.id)
        if player.current is None or player.now_playing_message is None:
            await ctx.send("🎵 Queue a song first (`/play`).")
            return
        state = await player.toggle_loop()
        await ctx.send(f"🔂 Loop {'on' if state else 'off'}.")

    @commands.hybrid_command(name="volume", description="Set playback volume (1–100).")
    @commands.guild_only()
    async def volume(self, ctx, percent: int):
        player = self._player(ctx.guild.id)
        level = await player.set_volume(max(1, min(percent, 100)))
        await ctx.send(f"🔊 Volume set to {round(level * 100)}%.")

    @commands.hybrid_command(name="queue",
                             description="Show the queue — or pass `auto` to generate a radio queue.")
    @commands.guild_only()
    async def queue(self, ctx, mode: str | None = None):
        """Show the queue, or generate a 📻 radio queue from the current track.

        `!queue auto` (or `/queue auto`) pulls a related-tracks radio for the
        song that's currently playing and adds it to the queue.
        """
        if mode is not None and mode.strip().lower() in ("auto", "radio", "seed"):
            await self._queue_auto(ctx, self._player(ctx.guild.id))
            return
        player = self._player(ctx.guild.id)
        if player.current is None and not player.queue:
            await ctx.send("🎵 The queue is empty. Add songs with `/play`.")
            return
        await ctx.send(embed=player.embed())

    async def _queue_auto(self, ctx, player: MusicPlayer, size: int = 8):
        """Generate a radio queue from the currently playing track."""
        if player.voice is None or not player.voice.is_connected():
            await ctx.send("🎧 I need to be in a voice channel first — use `/play`.")
            return
        if player.current is None or not player.current.video_id:
            await ctx.send("🎵 Play something first, then run `/queue auto` "
                           "to generate a 📻 radio queue.")
            return
        await ctx.defer()
        current = player.current
        try:
            seed_ids = await asyncio.to_thread(
                self._radio_seed_ids, current.video_id, size)
        except Exception as exc:
            LOG.warning("Auto-queue seed failed for %r: %s", current.title, exc)
            await ctx.send("⚠️ Couldn't generate a radio for the current track.")
            return
        if not seed_ids:
            await ctx.send("⚠️ No related tracks found for the current track.")
            return
        await ctx.send(f"📻 Building a radio queue from **{current.title}**…")
        tracks = await self._resolve_tracks(seed_ids)
        if not tracks:
            await ctx.send("⚠️ Couldn't resolve any of the radio tracks.")
            return
        for track in tracks:
            track.requester_id = ctx.author.id
            await player.enqueue(track)
        await player._update_panel()
        await ctx.send(
            f"➕ **{len(tracks)}** songs from the radio of **{current.title}** "
            f"added to the queue.")

    @commands.hybrid_command(name="nowplaying", description="Show the current track.")
    @commands.guild_only()
    async def nowplaying(self, ctx):
        player = self._player(ctx.guild.id)
        if player.current is None:
            await ctx.send("🎵 Nothing is playing.")
            return
        if player.now_playing_message is None:
            await self._send_panel(ctx, player)
        else:
            await player._update_panel()
            await ctx.send(embed=player.embed())

    # ── lyrics (LRCLIB) ─────────────────────────────────────────
    async def _lyrics_for(self, title: str, artist: str):
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": USER_AGENT},
            )
        query = f"{artist} {title}".strip() or title
        try:
            async with self._http.get(
                    "https://lrclib.net/api/search", params={"q": query}) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            return None
        if not data:
            return None
        track = data[0]
        text = track.get("plainLyrics") or track.get("syncedLyrics")
        if not text:
            return None
        return text, track.get("trackName") or title, track.get("artistName") or artist

    # ── lifecycle ───────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.id != self.bot.user.id:
            return
        if after.channel is None:
            self._cleanup(member.guild.id)


async def setup(bot):
    await bot.add_cog(Music(bot))