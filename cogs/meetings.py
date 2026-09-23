"""Private voice meeting rooms.

``/meeting create @alice @bob`` spins up a lockable voice channel that only
the creator and the tagged members can see or join. The room is registered in
the Appwrite settings (so it survives bot restarts) and deletes itself the
moment it becomes empty.
"""

import json
import logging

import discord
from discord.ext import commands

from data.store import store
from data.store import StoreError

LOG = logging.getLogger("bot.meetings")

SETTINGS_KEY = "meetings"
MAX_MEMBERS = 15  # sane cap on tagged members per private room


class Meetings(commands.Cog):
    """Private voice rooms that clean themselves up."""

    def __init__(self, bot):
        self.bot = bot
        # channel_id (int) -> {"owner": int, "members": [int, ...]}
        self.meetings: dict[int, dict] = {}

    # ── persistence ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        await self._load_meetings()

    async def _load_meetings(self) -> None:
        """Restore the room registry from the store after a restart."""
        try:
            raw = await store.get_setting(SETTINGS_KEY)
        except StoreError as exc:
            LOG.error("Meetings: could not load persisted rooms: %s", exc)
            return
        if not raw:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            LOG.error("Meetings: stored registry is corrupt; clearing it.")
            data = {}
        if not isinstance(data, dict):
            data = {}
        self.meetings = {
            int(cid): meta for cid, meta in data.items() if str(cid).isdigit() and isinstance(meta, dict)
        }
        # Rooms that vanished while we were offline are dropped from the registry.
        existing = {ch.id for guild in self.bot.guilds for ch in guild.voice_channels}
        gone = [cid for cid in self.meetings if cid not in existing]
        for cid in gone:
            self.meetings.pop(cid, None)
        if gone or len(self.meetings) != len(data):
            await self._save_meetings()
        LOG.info("Meetings: %d private room(s) restored", len(self.meetings))

    async def _save_meetings(self) -> None:
        try:
            await store.set_setting(SETTINGS_KEY, json.dumps(self.meetings))
        except StoreError as exc:
            LOG.error("Meetings: could not persist room registry: %s", exc)

    # ── helpers ──────────────────────────────────────────────────────
    def _find_channel(self, channel_id: int):
        for guild in self.bot.guilds:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                return channel
        return None

    async def _grant_access(self, channel: discord.VoiceChannel, member: discord.Member) -> None:
        await channel.set_permissions(
            member, view_channel=True, connect=True, speak=True
        )

    async def _destroy_meeting(self, channel_id: int) -> None:
        """Delete a room and drop it from the registry (empty rooms only)."""
        self.meetings.pop(channel_id, None)
        await self._save_meetings()
        channel = self._find_channel(channel_id)
        if channel is None:
            return
        try:
            await channel.delete(reason="Meeting room emptied — auto-cleaned")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            LOG.info("Meetings: cleanup of %s skipped: %s", channel_id, exc)

    # ── listeners ────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Delete tracked rooms the moment they empty out."""
        affected = set()
        if before.channel is not None:
            affected.add(before.channel.id)
        if after.channel is not None:
            affected.add(after.channel.id)
        for channel_id in affected:
            if channel_id not in self.meetings:
                continue
            channel = self._find_channel(channel_id)
            if channel is None:
                # Deleted out from under us — drop the stale entry.
                self.meetings.pop(channel_id, None)
                await self._save_meetings()
                continue
            try:
                occupants = len(channel.members)
            except (discord.Forbidden, discord.HTTPException):
                continue
            if occupants == 0:
                await self._destroy_meeting(channel_id)

    # ── commands ─────────────────────────────────────────────────────
    @commands.hybrid_group(name="meeting", description="Private voice meeting rooms.")
    @commands.guild_only()
    async def meeting(self, ctx):
        """Parent group — prints usage when invoked without a subcommand."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "🔒 **Private meeting rooms**\n"
                "`/meeting create @member… [name]` — spin up a private VC\n"
                "`/meeting end` — end your room early"
            )

    @meeting.command(name="create",
                     description="Create a private voice room with you + the tagged members.")
    @commands.guild_only()
    @commands.bot_has_permissions(manage_channels=True, move_members=True)
    async def meeting_create(self, ctx, members: commands.Greedy[discord.Member], *,
                             name: str | None = None):
        guild = ctx.guild
        author = ctx.author

        allowed = [author]
        seen = {author.id}
        for member in members:
            if member.id in seen or member == author or member.bot:
                continue
            seen.add(member.id)
            allowed.append(member)
        if len(allowed) - 1 > MAX_MEMBERS:
            await ctx.send(f"⛔ That's a crowd — max {MAX_MEMBERS} tagged members per room.")
            return

        room_name = (name or "").strip() or f"{author.display_name}'s meeting"
        room_name = room_name[:100]

        # Place the room in the same category as the author's current VC, if any.
        category = None
        if author.voice and author.voice.channel:
            category = author.voice.channel.category
        try:
            channel = await guild.create_voice_channel(
                f"🔒 {room_name}", category=category,
                reason=f"Private meeting room by {author.name}",
            )
        except discord.HTTPException as exc:
            await ctx.send(f"⚠️ Couldn't create the voice channel: {exc}")
            return

        # Lock it down: everyone loses access, the invitees get it back.
        await channel.set_permissions(guild.default_role, view_channel=False, connect=False)
        await channel.set_permissions(
            guild.me, view_channel=True, connect=True, manage_channels=True, move_members=True
        )
        for member in allowed:
            await self._grant_access(channel, member)

        self.meetings[channel.id] = {"owner": author.id, "members": [m.id for m in allowed]}
        await self._save_meetings()

        # Move everyone who is already in voice on this guild into the room.
        moved = []
        for member in allowed:
            if member.voice and member.voice.channel and member.voice.channel != channel:
                try:
                    await member.move_to(channel)
                    moved.append(member.display_name)
                except discord.HTTPException:
                    pass

        mentions = ", ".join(m.mention for m in allowed)
        message = (
            f"🔒 Private room `{channel.name}` ready for {mentions}.\n"
            "Only the people above can join — it deletes itself once empty."
        )
        if moved:
            message += "\nMoved in: " + ", ".join(moved)
        await ctx.send(message)

    @meeting.command(name="end", description="Delete your private meeting room.")
    @commands.guild_only()
    async def meeting_end(self, ctx):
        mine = [cid for cid, meta in self.meetings.items() if meta.get("owner") == ctx.author.id]
        if not mine:
            await ctx.send("🔒 You don't have an active meeting room.")
            return
        for cid in mine:
            channel = self._find_channel(cid)
            if channel is not None:
                try:
                    await channel.delete(reason=f"Meeting ended by {ctx.author.name}")
                except discord.Forbidden:
                    await ctx.send("⛔ I couldn't delete one of your rooms (missing permission).")
                    continue
            self.meetings.pop(cid, None)
        await self._save_meetings()
        await ctx.send("🔒 Meeting room(s) ended and deleted.")


async def setup(bot):
    await bot.add_cog(Meetings(bot))