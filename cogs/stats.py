import logging
import time

import discord
from discord.ext import commands, tasks

import config
from data.store import StoreError, store

LOG = logging.getLogger("bot.stats")

ACTIVE_STATUSES = (discord.Status.online, discord.Status.idle, discord.Status.dnd)


def fmt_seconds(seconds: float) -> str:
    """Format a duration as 00h 00m 00s."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}h {minutes:02d}m {secs:02d}s"


class Stats(commands.Cog):
    """Unified stats tracking: messages, voice time, online presence.

    High-frequency events are accumulated in memory and flushed to the Appwrite
    store every ``config.DASHBOARD_REFRESH_SECONDS`` seconds, so per-message /
    per-second writes never reach the database. The dashboard reads the
    persisted counters instead of scanning channel history.
    """

    def __init__(self, bot):
        self.bot = bot
        # Pending accumulators (since the last flush).
        self.total_messages = 0          # global
        self.total_voice_seconds = 0.0   # global
        self.message_deltas = {}         # user_id -> messages since last flush
        self.voice_seconds = {}          # user_id -> seconds since last flush
        self.voice_join = {}             # user_id -> monotonic timestamp of join
        self.voice_credited = {}         # user_id -> seconds already sent to the store
        self.voice_credited_stage = {}   # next credited values, committed only on successful flush

    # ── events ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        self.total_messages += 1
        self.message_deltas[message.author.id] = self.message_deltas.get(message.author.id, 0) + 1

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        now = time.monotonic()

        # Close the previous segment when the channel changed or the user left.
        if before.channel != after.channel and member.id in self.voice_join:
            self._close_voice_segment(member.id, now)

        # Open a new segment whenever the user is in a voice channel.
        if after.channel is not None and member.id not in self.voice_join:
            self.voice_join[member.id] = now

    def _close_voice_segment(self, user_id: int, now: float) -> None:
        """Close a session and add the *unflushed* remainder to the totals.

        ``voice_credited`` tracks how many of this session's seconds have
        already been persisted by earlier flushes, so closing a session never
        double-counts or drops previously flushed time.
        """
        start = self.voice_join.pop(user_id, None)
        if start is None:
            return
        credited = self.voice_credited.pop(user_id, 0.0)
        self.voice_credited_stage.pop(user_id, None)
        delta = (now - start) - credited
        self.total_voice_seconds += delta
        self.voice_seconds[user_id] = self.voice_seconds.get(user_id, 0.0) + delta

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        # Adopt voice sessions that were open before we connected (and close
        # stale ones from a previous connection). Without this, members who
        # are already in a voice channel at startup never get a join event
        # and their voice time stays 00:00:00.
        self._sync_voice_sessions()
        # Reconnect guard: the loop may already be running across reconnects.
        if not self.flush_loop.is_running():
            self.flush_loop.start()

    def _sync_voice_sessions(self):
        """Reconcile in-memory sessions against the authoritative voice states.

        ``on_voice_state_update`` only fires on *changes*, so anyone sitting
        in a voice channel when the bot connects never triggers a join event
        and would show 00:00:00 forever. The ready snapshot
        (``guild.voice_states``) is authoritative, so adopt those sessions and
        close off any leftovers whose members left during a disconnect (their
        time is credited up to the moment we found out, not the next join).
        """
        now = time.monotonic()
        still_in_voice = set()
        for guild in getattr(self.bot, "guilds", []):
            for user_id, state in getattr(guild, "voice_states", {}).items():
                if state.channel is None:
                    continue
                still_in_voice.add(user_id)
                if user_id not in self.voice_join:
                    self.voice_join[user_id] = now
        # Close sessions whose members left while we were away/offline.
        for uid in [uid for uid in self.voice_join if uid not in still_in_voice]:
            self._close_voice_segment(uid, now)

    # ── flushing ───────────────────────────────────────────────
    @tasks.loop(seconds=config.DASHBOARD_REFRESH_SECONDS)
    async def flush_loop(self):
        await self.flush()

    async def flush(self):
        """Persist accumulated counters and per-member deltas in one pass."""
        activity, messages, voice_seconds = self._drain()
        try:
            if activity:
                await store.flush_member_activity(activity)
            if messages or voice_seconds:
                await store.bump_counters(messages=messages, voice_seconds=voice_seconds)
        except StoreError as exc:
            LOG.error("Stats flush failed: %s", exc)
            return
        # The store now holds every second counted so far — commit the open
        # sessions' credited baseline so the next flush only sends the delta.
        # Stale uids (closed mid-flush) are dropped so a later rejoin starts
        # from a clean slate.
        for uid, dur in self.voice_credited_stage.items():
            if uid in self.voice_join:
                self.voice_credited[uid] = dur
        self.voice_credited_stage.clear()

    def _drain(self):
        now = time.monotonic()
        # Persist open sessions incrementally: queue the seconds accumulated
        # since the last flush (deltas), staged so a failed store write never
        # advances the credited baseline.
        for uid, start in self.voice_join.items():
            dur = now - start
            delta = dur - self.voice_credited.get(uid, 0.0)
            self.total_voice_seconds += delta
            self.voice_seconds[uid] = self.voice_seconds.get(uid, 0.0) + delta
            self.voice_credited_stage[uid] = dur
        activity = {}
        for uid, n in self.message_deltas.items():
            activity[uid] = {"messages": n}
        for uid, secs in self.voice_seconds.items():
            activity.setdefault(uid, {})["voice_seconds"] = round(secs, 1)
        self.message_deltas.clear()
        self.voice_seconds.clear()
        total_messages, self.total_messages = self.total_messages, 0
        total_voice, self.total_voice_seconds = self.total_voice_seconds, 0.0
        return activity, total_messages, total_voice

    # ── commands ───────────────────────────────────────────────
    @commands.hybrid_command(name="total_messages", description="Total messages sent in the server.")
    async def total_messages(self, ctx):
        """Persisted total plus anything still pending in memory."""
        try:
            counters = await store.get_counters()
        except StoreError:
            counters = {}
        total = int(counters.get("total_messages", 0)) + self.total_messages
        await ctx.send(f"Total messages sent in the server: {total}")

    async def view_total_voice_seconds(self) -> float:
        """Persisted total + pending closed segments + unflushed open seconds.

        The dashboard renders the same number so the board and the command
        never disagree. Open sessions are counted as ``now - start - credited``
        so already-flushed seconds are never added twice.
        """
        try:
            counters = await store.get_counters()
        except StoreError:
            counters = {}
        return (
            float(counters.get("total_voice_seconds", 0.0))
            + self.total_voice_seconds
            + self._open_unflushed_seconds()
        )

    def _open_unflushed_seconds(self) -> float:
        """Live, not-yet-flushed seconds of every open voice session."""
        now = time.monotonic()
        return sum(
            (now - start) - self.voice_credited.get(uid, 0.0)
            for uid, start in self.voice_join.items()
        )

    @commands.hybrid_command(name="total_voice_time", description="Total voice time spent in the server.")
    async def total_voice_time(self, ctx):
        """Persisted total plus anything still pending in memory or live."""
        total = await self.view_total_voice_seconds()
        await ctx.send(f"Total voice time in the server: {fmt_seconds(total)}")

    @commands.hybrid_command(name="current_voice_time",
                             description="Time in your voice channel this session.")
    async def current_voice_time(self, ctx):
        if not ctx.author.voice:
            await ctx.send("You are not in a voice channel!")
            return
        channel = ctx.author.voice.channel
        now = time.monotonic()
        total = 0.0
        for member in channel.members:
            start = self.voice_join.get(member.id)
            if start is not None:
                total += now - start
        await ctx.send(
            f"Total time spent in {channel.name} so far: {fmt_seconds(total)}"
        )

    @commands.hybrid_command(name="online_members", description="Number of members currently online/idle/dnd.")
    async def online_members(self, ctx):
        online = sum(1 for m in ctx.guild.members if m.status in ACTIVE_STATUSES)
        await ctx.send(f"Number of online members: {online}")


async def setup(bot):
    await bot.add_cog(Stats(bot))