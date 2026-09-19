"""Club cells — place members into a cell.

A cell is the free-text ``cell`` field on a member's linked club account (set
via ``/setprofile``); it is a soft grouping, not a Discord role. This cog adds
the self-service counterpart to that field: ``/cell add`` lets a Cell Chief pull
tagged members into their *own* cell, while leadership and bot staff can place
anyone into any cell.

Adding a member never demotes a rank — a plain Core Member is promoted to Cell
Member, everyone above keeps their club role. A member already in a different
cell is moved, and the move is reported back.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs._perms import is_bot_admin
from cogs._scopes import require_scope, scopes_for
from data.store import StoreError, store

LOG = logging.getLogger("bot.cells")

# Matches the ``cell`` attribute size in the bot_members schema.
CELL_MAX = 64


def _clean_cell(value) -> str:
    """Normalise a cell name: collapse whitespace, drop stray padding."""
    return " ".join(str(value or "").split())


class Cells(commands.Cog):
    """Cells — group members under a cell and its chief."""

    def __init__(self, bot):
        self.bot = bot

    # ── lookups ────────────────────────────────────────────────
    @staticmethod
    async def _record(user_id: int) -> dict:
        try:
            return (await store.get_member(user_id)) or {}
        except StoreError:
            return {}

    async def _own_cell(self, member: discord.Member) -> str:
        return _clean_cell((await self._record(member.id)).get("cell"))

    async def _can_choose(self, member: discord.Member) -> bool:
        """Can this member target an arbitrary cell (not just their own)?

        True for bot staff and for anyone whose linked club role carries
        ``cells.manage`` (Vice President / President / Archon). Cell Chiefs
        deliberately fall short here and stay bound to their own cell.
        """
        if is_bot_admin(member):
            return True
        record = await self._record(member.id)
        scopes = scopes_for(str(record.get("club_role") or "").lower(),
                            is_member=member)
        return "cells.manage" in scopes

    async def _known_cells(self) -> list[str]:
        """Every distinct cell already used by a member (for autocomplete)."""
        try:
            members = await store.list_members(limit=200)
        except StoreError:
            return []
        return sorted({c for c in (_clean_cell(m.get("cell")) for m in members) if c})

    # ── /cell ──────────────────────────────────────────────────
    @commands.hybrid_group(name="cell", description="Cell membership.",
                           invoke_without_command=True)
    @commands.guild_only()
    async def cell(self, ctx):
        """Usage: /cell add @members… [cell]."""
        await ctx.send(
            "🧩 **/cell** usage\n"
            "`add @members… [cell]` — place members into a cell\n"
            "• Cell Chiefs add to their **own** cell automatically.\n"
            "• Leadership (VP/President) and bot staff can name any cell."
        )

    @cell.command(name="add", description="Add one or more members to a cell.")
    @commands.guild_only()
    @require_scope("cells.assign")
    async def cell_add(self, ctx, members: commands.Greedy[discord.Member],
                       cell: str = ""):
        """Place tagged members into a cell.

        Cell Chiefs place members into their own cell and cannot pick another;
        bot staff and members holding ``cells.manage`` may pass ``cell`` to
        choose any cell. A member already in a different cell is moved.
        """
        desired = _clean_cell(cell)

        if await self._can_choose(ctx.author):
            target = desired or await self._own_cell(ctx.author)
            if not target:
                await ctx.send(
                    "⚠️ No cell given — pick one, e.g. "
                    "`/cell add @ana Programming`."
                )
                return
        else:
            target = await self._own_cell(ctx.author)
            if not target:
                await ctx.send(
                    "⚠️ You don't have a cell on your profile yet — "
                    "ask leadership to set one."
                )
                return
            if desired and desired.lower() != target.lower():
                await ctx.send(
                    f"🔒 You can only add members to your own cell (**{target}**)."
                )
                return

        if len(target) > CELL_MAX:
            await ctx.send(
                f"⚠️ That cell name is too long (max {CELL_MAX} characters)."
            )
            return

        targets = [m for m in members if not m.bot]
        if not targets:
            await ctx.send("⚠️ Tag at least one member to add.")
            return

        added, moved, already, failed = [], [], [], []
        for member in targets:
            record = await self._record(member.id)
            previous = _clean_cell(record.get("cell"))
            payload = {"cell": target}
            role = str(record.get("club_role") or "").lower()
            # Promote only a blank / Core Member account; never demote.
            if role in ("", "core_member"):
                payload["club_role"] = "cell_member"
            try:
                await store.merge_member(member.id, payload)
            except StoreError as exc:
                LOG.warning("cell add failed for %s: %s", member.id, exc)
                failed.append(member.display_name)
                continue

            if not previous:
                added.append(member)
            elif previous.lower() == target.lower():
                already.append(member)
            else:
                moved.append((member, previous))

        lines = [f"🧩 Added to **{target}**:"]
        if added:
            lines.append("• " + ", ".join(m.mention for m in added))
        for member, previous in moved:
            lines.append(f"• {member.mention} — moved from **{previous}**")
        if already:
            lines.append("• " + ", ".join(f"{m.mention} (already there)"
                                          for m in already))
        if failed:
            lines.append("⚠️ Couldn't update: " + ", ".join(failed))
        await ctx.send("\n".join(lines))

    @cell_add.autocomplete("cell")
    async def _cell_autocomplete(self, interaction: discord.Interaction,
                                 current: str):
        """Offer existing cell names; chiefs only ever see their own."""
        if await self._can_choose(interaction.user):
            names = await self._known_cells()
        else:
            own = await self._own_cell(interaction.user)
            names = [own] if own else []
        needle = current.lower()
        return [app_commands.Choice(name=n, value=n)
                for n in names if needle in n.lower()][:25]


async def setup(bot):
    await bot.add_cog(Cells(bot))
