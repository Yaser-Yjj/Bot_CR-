import logging
from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands

import config
from data.store import store
from data.store import StoreError
from cogs.onboarding import cursive_nickname
from cogs._ui import ConfirmView, PaginatorView
from cogs._perms import mod_perms

LOG = logging.getLogger("bot.moderation")


class Moderation(commands.Cog):
    """Kick, ban, timeout, warn — every action lands in the Appwrite modlog."""

    def __init__(self, bot):
        self.bot = bot

    # ── helpers ────────────────────────────────────────────────
    async def _can_target(self, ctx: commands.Context, member: discord.Member) -> bool:
        """Refuse obviously invalid or hierarchy-blocked targets."""
        if member == ctx.author:
            await ctx.send("⛔ You can't moderate yourself.")
            return False
        if member == self.bot.user:
            await ctx.send("Nice try. I'm not doing that to myself. 🤖")
            return False
        if ctx.author != ctx.guild.owner and member.top_role >= ctx.author.top_role:
            await ctx.send(f"⛔ **{member.display_name}** has a role equal to or higher than yours.")
            return False
        if member.top_role >= ctx.guild.me.top_role:
            await ctx.send("⛔ My role isn't high enough to moderate that member.")
            return False
        return True

    async def _log(self, ctx: commands.Context, action: str, member: discord.Member, reason: str):
        try:
            await store.log_moderation(
                action=action,
                target_id=member.id,
                target_name=member.display_name,
                moderator_id=ctx.author.id,
                moderator_name=ctx.author.display_name,
                reason=reason,
            )
        except StoreError as exc:
            LOG.error("Modlog write failed: %s", exc)

    # ── purge (from the legacy suite) ──────────────────────────
    @commands.hybrid_command(name="del", description="Delete the last N messages (1–100).")
    @mod_perms(manage_messages=True)
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def delete_messages(self, ctx, number: int):
        """Delete the last N messages in this channel plus the command itself."""
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.send("This command can only be used in text channels.")
            return

        number = max(1, min(number, 100))
        try:
            deleted = await ctx.channel.purge(limit=number, before=ctx.message)
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            await ctx.send(f"Deleted {len(deleted)} message(s) in #{ctx.channel.name}.", delete_after=3)
        except discord.Forbidden:
            await ctx.send("I don't have permission to delete messages here.", delete_after=5)
        except discord.HTTPException as exc:
            await ctx.send(f"Failed to delete messages: {exc}", delete_after=5)

    # ── kick ───────────────────────────────────────────────────
    @commands.hybrid_command(name="kick", description="Kick a member from the server.")
    @mod_perms(kick_members=True)
    
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self._can_target(ctx, member):
            return

        async def do_kick(interaction: discord.Interaction):
            try:
                await member.kick(reason=f"{ctx.author.name}: {reason}")
            except discord.Forbidden:
                await interaction.followup.send(
                    "⛔ I don't have permission to kick that member.", ephemeral=True)
                return
            except discord.HTTPException as exc:
                await interaction.followup.send(f"⚠️ Kick failed: {exc}", ephemeral=True)
                return
            await interaction.followup.send(f"👢 Kicked **{member.display_name}** — {reason}")
            await self._log(ctx, "kick", member, reason)

        view = ConfirmView(do_kick)
        await ctx.send(f"👢 Kick **{member.display_name}**? — {reason}", view=view)

    # ── ban / unban ────────────────────────────────────────────
    @commands.hybrid_command(name="ban", description="Ban a member from the server.")
    @mod_perms(ban_members=True)
    
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if not await self._can_target(ctx, member):
            return

        async def do_ban(interaction: discord.Interaction):
            try:
                await member.ban(reason=f"{ctx.author.name}: {reason}", delete_message_days=0)
            except discord.Forbidden:
                await interaction.followup.send(
                    "⛔ I don't have permission to ban that member.", ephemeral=True)
                return
            except discord.HTTPException as exc:
                await interaction.followup.send(f"⚠️ Ban failed: {exc}", ephemeral=True)
                return
            await interaction.followup.send(f"🔨 Banned **{member.display_name}** — {reason}")
            await self._log(ctx, "ban", member, reason)

        view = ConfirmView(do_ban)
        await ctx.send(f"🔨 Ban **{member.display_name}**? — {reason}", view=view)

    @commands.hybrid_command(name="unban", description="Unban a user by their ID.")
    @mod_perms(ban_members=True)
    
    async def unban(self, ctx, user_id: int, *, reason: str = "No reason provided"):
        try:
            await ctx.guild.unban(discord.Object(id=user_id), reason=f"{ctx.author.name}: {reason}")
        except discord.NotFound:
            await ctx.send("⚠️ That user isn't banned.")
            return
        await ctx.send(f"🔓 Unbanned <@{user_id}> — {reason}")

    # ── timeout / mute (shared core) ──────────────────────────
    async def _apply_timeout(self, ctx, member, minutes, reason, action, label):
        """Core timeout/mute — permission-checked, capped, modlog-backed."""
        if not await self._can_target(ctx, member):
            return
        minutes = max(1, min(minutes, 10080))  # Discord caps timeouts at 28 days
        try:
            await member.timeout(discord.utils.utcnow() + timedelta(minutes=minutes),
                                 reason=f"{ctx.author.name}: {reason}")
        except discord.Forbidden:
            await ctx.send("⛔ I don't have permission to timeout that member.")
            return
        await ctx.send(f"🔇 {label} **{member.display_name}** for {minutes} min — {reason}")
        await self._log(ctx, action, member, reason)

    async def _remove_timeout(self, ctx, member, reason, action, label):
        """Core untimeout/unmute — shares the same guardrails."""
        if not await self._can_target(ctx, member):
            return
        try:
            await member.timeout(None, reason=f"{ctx.author.name}: {reason}")
        except discord.Forbidden:
            await ctx.send("⛔ I don't have permission to modify that member's timeout.")
            return
        await ctx.send(f"{label} for **{member.display_name}**")
        await self._log(ctx, action, member, reason)

    @commands.hybrid_command(name="timeout", description="Timeout a member (minutes).")
    @mod_perms(moderate_members=True)
    
    async def timeout(self, ctx, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
        await self._apply_timeout(ctx, member, minutes, reason, "timeout", "Timed out")

    @commands.hybrid_command(name="untimeout", description="Remove a member's timeout.")
    @mod_perms(moderate_members=True)
    
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = "Timeout lifted"):
        await self._remove_timeout(ctx, member, reason, "untimeout", "Timeout lifted")

    @commands.hybrid_command(name="mute",
                             description="Mute a member for N minutes (Discord timeout).")
    @mod_perms(moderate_members=True)
    
    async def mute(self, ctx, member: discord.Member, minutes: int = 60, *,
                   reason: str = "No reason provided"):
        await self._apply_timeout(ctx, member, minutes, reason, "mute", "Muted")

    @commands.hybrid_command(name="unmute",
                             description="Remove a member's mute (timeout).")
    @mod_perms(moderate_members=True)
    
    async def unmute(self, ctx, member: discord.Member, *, reason: str = "Mute lifted"):
        await self._remove_timeout(ctx, member, reason, "unmute", "Unmuted")

    # ── warn ───────────────────────────────────────────────────
    @commands.hybrid_command(name="warn", description="Warn a member (recorded in the modlog).")
    @mod_perms(moderate_members=True)
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        if member.bot:
            await ctx.send("⚠️ I don't track warnings for bots.")
            return
        try:
            await store.increment_member(member.id, "warnings", 1, bootstrap={"username": member.name})
        except StoreError as exc:
            LOG.error("Warn counter update failed: %s", exc)
        await ctx.send(f"⚠️ Warned **{member.display_name}** — {reason}")
        await self._log(ctx, "warn", member, reason)

    # ── fixname ──────────────────────────────────────────────
    @commands.hybrid_command(name="fixname",
                             description="Set a member's cursive nickname — pass their real full name (optional).")
    @mod_perms(manage_nicknames=True)
    async def fixname(self, ctx, member: discord.Member, *, name: Optional[str] = None):
        """Reset a nickname to the cursive form of a real full name.

        Pass the member's real full name to override the stored record — the
        name is persisted so future re-applies reuse it. Without a name, the
        real name collected during DM onboarding is used, falling back to the
        current display name (e.g. legacy members who never onboarded).
        """
        if member == self.bot.user:
            await ctx.send("Nice try. I like my name. 🤖")
            return
        if name is not None:
            given = name.strip()
            if not given:
                await ctx.send("⚠️ The name can't be empty.")
                return
            try:
                await store.merge_member(member.id, {"real_name": given})
            except StoreError as exc:
                LOG.warning("fixname: could not persist real_name for %s: %s", member.id, exc)
            source = given
        else:
            try:
                record = await store.get_member(member.id)
            except StoreError:
                record = None
            source = (record or {}).get("real_name") or member.display_name.strip() or member.name
        nick = cursive_nickname(source)
        if not nick:
            await ctx.send("⚠️ Couldn't build a name for that member.")
            return
        try:
            await member.edit(nick=nick, reason=f"fixname by {ctx.author.name}")
        except discord.Forbidden:
            await ctx.send("⛔ I need the *Manage Nicknames* permission for that.")
            return
        except discord.HTTPException as exc:
            await ctx.send(f"⚠️ Failed to set the nickname: {exc}")
            return
        await ctx.send(f"✏️ Fixed **{member.display_name}** → `{nick}`")
        saved = " (real name saved)" if name is not None else ""
        await self._log(ctx, "fixname", member, f"nickname reset to the cursive real-name form{saved}")

    # ── role management ───────────────────────────────────────
    async def _check_role(self, ctx, role: discord.Role) -> bool:
        """Shared guard for role edits: integration roles and hierarchy."""
        if role.managed:
            await ctx.send(f"⛔ `{role.name}` is an integration-managed role — I can't assign it.")
            return False
        if role >= ctx.guild.me.top_role:
            await ctx.send("⛔ My top role isn't high enough to manage `" + role.name + "`.")
            return False
        return True

    @commands.hybrid_command(name="addrole", description="Give a role to one or more members.")
    @commands.guild_only()
    @mod_perms(manage_roles=True)
    async def addrole(self, ctx, role: discord.Role, members: commands.Greedy[discord.Member]):
        """Tag the role, then tag who gets it — `!addrole @role @a @b`."""
        if not await self._check_role(ctx, role):
            return
        targets = [m for m in members if not m.bot and m != self.bot.user]
        if not targets:
            await ctx.send("⚠️ Tag at least one member to receive the role.")
            return
        added = skipped = 0
        for member in targets:
            if role in member.roles:
                skipped += 1
                continue
            try:
                await member.add_roles(role, reason=f"addrole by {ctx.author.name}")
                added += 1
            except discord.HTTPException:
                skipped += 1
        extra = f" ({skipped} skipped)" if skipped else ""
        await ctx.send(f"✅ Added {role.mention} to **{added}** member(s){extra}.")
        if added:
            await self._log(ctx, "addrole", targets[0], f"role={role.name} members={added}")

    @commands.hybrid_command(name="removerole", description="Remove a role from one or more members.")
    @commands.guild_only()
    @mod_perms(manage_roles=True)
    async def removerole(self, ctx, role: discord.Role, members: commands.Greedy[discord.Member]):
        """Tag the role, then tag who loses it — `!removerole @role @a @b`."""
        if not await self._check_role(ctx, role):
            return
        targets = [m for m in members if not m.bot and m != self.bot.user]
        if not targets:
            await ctx.send("⚠️ Tag at least one member to strip the role from.")
            return
        removed = skipped = 0
        for member in targets:
            if role not in member.roles:
                skipped += 1
                continue
            try:
                await member.remove_roles(role, reason=f"removerole by {ctx.author.name}")
                removed += 1
            except discord.HTTPException:
                skipped += 1
        extra = f" ({skipped} skipped)" if skipped else ""
        await ctx.send(f"✅ Removed {role.mention} from **{removed}** member(s){extra}.")
        if removed:
            await self._log(ctx, "removerole", targets[0], f"role={role.name} members={removed}")

    @commands.hybrid_command(
        name="setlead",
        description="Replace all holders of a leadership role with the tagged members.",
    )
    @commands.guild_only()
    @mod_perms(manage_roles=True)
    async def setlead(self, ctx, role: discord.Role, members: commands.Greedy[discord.Member]):
        """Set the leadership team: strips the role from everyone, keeps only
        the members you tag. Restricted to the configured leader roles
        (config.LEADER_ROLES — Lead / Vice President / President)."""
        allowed = {name.strip().lower() for name in config.LEADER_ROLES}
        if role.name.strip().lower() not in allowed:
            await ctx.send(
                f"⛔ `{role.name}` isn't a leadership role. Use one of: **{', '.join(config.LEADER_ROLES)}**."
            )
            return
        if not await self._check_role(ctx, role):
            return
        targets = [m for m in members if not m.bot and m != self.bot.user]
        target_ids = {m.id for m in targets}
        if not targets:
            await ctx.send("⚠️ Tag at least one member as the new holder(s).")
            return
        revoked = []
        for member in ctx.guild.members:
            if member.bot or member.id in target_ids or role not in member.roles:
                continue
            revoked.append(member.display_name)
            try:
                await member.remove_roles(role, reason=f"setlead by {ctx.author.name}")
            except discord.HTTPException:
                pass
        granted = []
        for member in targets:
            if role not in member.roles:
                granted.append(member.display_name)
                try:
                    await member.add_roles(role, reason=f"setlead by {ctx.author.name}")
                except discord.HTTPException:
                    granted.pop()
        msg = f"👑 {role.mention} → **{', '.join(granted) if granted else 'no change'}**"
        if revoked:
            msg += f"\n↩️ Removed from: {', '.join(revoked)}"
        await ctx.send(msg)
        await self._log(ctx, "setlead", ctx.author,
                        f"role={role.name} granted={len(granted)} revoked={len(revoked)}")

    # ── channel controls ───────────────────────────────────────
    @commands.hybrid_command(name="slowmode", description="Set the channel slowmode (seconds).")
    @commands.guild_only()
    @mod_perms(manage_channels=True)
    async def slowmode(self, ctx, seconds: int, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        delay = max(0, min(int(seconds), 21600))
        try:
            await channel.edit(slowmode_delay=delay)
        except discord.Forbidden:
            await ctx.send("⛔ I need *Manage Channels* to change slowmode.")
            return
        if delay:
            await ctx.send(f"🐌 Slowmode in {channel.mention} set to **{delay}s**.")
        else:
            await ctx.send(f"🐌 Slowmode removed in {channel.mention}.")

    @commands.hybrid_command(name="lock", description="Lock a channel (members can't send).")
    @commands.guild_only()
    @mod_perms(manage_channels=True)
    async def lock(self, ctx, channel: discord.TextChannel = None):
        await self._set_lock(ctx, channel or ctx.channel, locked=True)

    @commands.hybrid_command(name="unlock", description="Unlock a channel.")
    @commands.guild_only()
    @mod_perms(manage_channels=True)
    async def unlock(self, ctx, channel: discord.TextChannel = None):
        await self._set_lock(ctx, channel or ctx.channel, locked=False)

    async def _set_lock(self, ctx, channel: discord.TextChannel, *, locked: bool):
        try:
            await channel.set_permissions(
                ctx.guild.default_role,
                send_messages=False if locked else None,
                reason=f"{'lock' if locked else 'unlock'} by {ctx.author.name}",
            )
        except discord.Forbidden:
            await ctx.send("⛔ I need *Manage Channels* to change permissions there.")
            return
        emoji = "🔒" if locked else "🔓"
        state = "locked" if locked else "unlocked"
        await ctx.send(f"{emoji} {channel.mention} is now **{state}**.")

    # ── modlog ─────────────────────────────────────────────────
    @commands.hybrid_command(name="modlog", description="Show recent moderation actions.")
    @mod_perms(moderate_members=True)
    async def modlog(self, ctx, limit: int = 50):
        limit = max(10, min(limit, 100))
        try:
            entries = await store.list_modlog(limit=limit)
        except StoreError as exc:
            await ctx.send(f"⚠️ Could not read the modlog: {exc}")
            return
        if not entries:
            await ctx.send("📭 No moderation actions recorded yet.")
            return
        lines = []
        for e in entries:
            when = str(e.get("created_at", ""))[:16].replace("T", " ")
            target = e.get("target_name") or f"<@{e.get('target_id')}>"
            mod = e.get("moderator_name") or "staff"
            reason = e.get("reason") or ""
            lines.append(f"`{when}` **{e.get('action', '?')}** {target} — {reason} *(by {mod})*")
        page_size = 10
        pages = [
            "🛡️ **Recent moderation actions**\n" + "\n".join(lines[i:i + page_size])
            for i in range(0, len(lines), page_size)
        ]
        if len(pages) == 1:
            await ctx.send(pages[0])
        else:
            await ctx.send(pages[0], view=PaginatorView(pages))


async def setup(bot):
    await bot.add_cog(Moderation(bot))