"""Club competitions — one registration list, shared with the app.

``/competitions`` lists upcoming events anyone can join; ``/competition``
shows details with Register/Unregister buttons that write straight to
Appwrite (capacity enforced server-side), so the app and Discord always agree.
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from data.store import store
from data.store import StoreError
from cogs._dates import days_until, fmt_date, slugify
from cogs._scopes import require_scope

LOG = logging.getLogger("bot.competitions")


def _comp_embed(comp: dict) -> discord.Embed:
    regs = comp.get("registered") or []
    capacity = comp.get("capacity")
    cap_text = f"/ {capacity}" if capacity else ""
    embed = discord.Embed(
        title=f"🏆 {comp.get('name', '?')}",
        description=comp.get("description") or "",
        color=discord.Color.magenta(),
    )
    embed.add_field(name="📅 Date", value=fmt_date(comp.get("date")), inline=True)
    embed.add_field(name="📍 Location", value=comp.get("location") or "TBD", inline=True)
    status = "Registration full" if capacity and len(regs) >= capacity else "Open" if capacity or comp.get("date") else "—"
    embed.add_field(name="Registration", value=status, inline=True)
    embed.add_field(name="👥 Registered", value=f"{len(regs)}{cap_text}", inline=True)
    return embed


class CompetitionView(discord.ui.View):
    """Register / unregister / participants for one competition."""

    def __init__(self, cog, slug: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.slug = slug

    @discord.ui.button(label="✅ Register", style=discord.ButtonStyle.success,
                       custom_id="comp:register")
    async def register(self, interaction, button):
        await self._set_registration(interaction, True)

    @discord.ui.button(label="⬜ Unregister", style=discord.ButtonStyle.secondary,
                       custom_id="comp:unregister")
    async def unregister(self, interaction, button):
        await self._set_registration(interaction, False)

    @discord.ui.button(label="👥 Participants", style=discord.ButtonStyle.secondary,
                       custom_id="comp:participants")
    async def participants(self, interaction, button):
        comp = await self.cog._get(self.slug)
        if comp is None:
            await interaction.response.send_message("⚠️ Competition not found.",
                                                    ephemeral=True)
            return
        regs = comp.get("registered") or []
        names = []
        for raw in regs:
            member = interaction.guild.get_member(int(raw))
            names.append(member.display_name if member else f"<@{raw}>")
        if not names:
            await interaction.response.send_message(
                "👥 Nobody has registered yet — be the first!", ephemeral=True)
            return
        front = ", ".join(names[:25])
        more = f" … +{len(names) - 25}" if len(names) > 25 else ""
        await interaction.response.send_message(
            f"👥 **{comp.get('name')}** — {len(names)} registered:\n{front}{more}",
            ephemeral=True)

    async def _set_registration(self, interaction, registered: bool):
        try:
            ok, message = await store.set_registration(self.slug, interaction.user.id,
                                                       registered)
        except StoreError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        if not ok:
            if message == "full":
                await interaction.response.send_message(
                    "⛔ This competition is full! Check back later.", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"ℹ️ You're already {message.replace('already ', '')}.",
                    ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=_comp_embed((await self.cog._get(self.slug))),
            view=self)
        await interaction.followup.send(
            f"{'✅' if registered else '⬜'} {message.capitalize()} — "
            f"**{await self.cog._display_name(self.slug)}**.", ephemeral=True)


class Competitions(commands.Cog):
    """List, register for and manage robotics competitions."""

    def __init__(self, bot):
        self.bot = bot

    async def _get(self, slug: str):
        try:
            return await store.get_competition(slug)
        except StoreError:
            return None

    async def _display_name(self, slug: str) -> str:
        doc = await self._get(slug)
        return (doc or {}).get("name", slug.replace("_", " ").title())

    # ── commands ───────────────────────────────────────────────
    @commands.hybrid_command(name="competitions", description="Upcoming competitions.")
    @commands.guild_only()
    @require_scope("competitions.read")
    async def competitions(self, ctx):
        """Everyone: what's coming up in the club's competitive calendar."""
        try:
            comps = await store.list_competitions()
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't load competitions: {exc}")
            return
        upcoming = [c for c in comps
                    if days_until(c.get("date")) is None or days_until(c.get("date")) >= 0]
        upcoming.sort(key=lambda c: str(c.get("date") or "9999"))
        if not upcoming:
            await ctx.send("🏆 No upcoming competitions. Staff: `/competition create`.")
            return
        lines = []
        for c in upcoming[:12]:
            cap = f"/{c['capacity']}" if c.get("capacity") else ""
            lines.append(f"🏆 **{c.get('name')}** · {fmt_date(c.get('date'))} · "
                         f"📍 {c.get('location') or 'TBD'}\n"
                         f"   👥 {len(c.get('registered') or [])}{cap} · "
                         f"`/competition {c.get('name')}`")
        await ctx.send("🏆 **Upcoming competitions**\n\n" + "\n".join(lines))

    @commands.hybrid_group(name="competition", description="Competition details.",
                           invoke_without_command=True, fallback="view")
    @commands.guild_only()
    @require_scope("competitions.read")
    async def competition(self, ctx, name: str):
        """Details + register for a competition (writes back to Appwrite)."""
        slug = slugify(name)
        comp = await self._get(slug)
        if comp is None:
            # fuzzy fallback: first name containing the query
            try:
                for candidate in await store.list_competitions():
                    if slugify(candidate.get("name", "")) == slug or \
                       slugify(candidate.get("name", "")) in slug or \
                       slug in slugify(candidate.get("name", "")):
                        comp = candidate
                        break
            except StoreError:
                pass
        if comp is None:
            await ctx.send(f"⚠️ No competition matching **{name}**. "
                           "Try `/competitions`.")
            return
        await ctx.send(embed=_comp_embed(comp),
                       view=CompetitionView(self, slugify(comp["name"])))

    @competition.command(name="create", description="Create a competition (leadership).")
    @commands.guild_only()
    @require_scope("competitions.manage")
    async def competition_create(self, ctx, name: str, date: str = "",
                                 location: str = "", capacity: int = 0,
                                 description: str = ""):
        """Register a competition so the members can sign up."""
        slug = slugify(name)
        if await self._get(slug) is not None:
            await ctx.send(f"⚠️ A competition named **{name}** already exists.")
            return
        comp = {
            "name": name.strip(),
            "date": date.strip(),
            "location": location.strip(),
            "capacity": capacity if capacity > 0 else None,
            "registered": [],
            "description": description.strip(),
            "created_by": str(ctx.author.id),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await store.save_competition(slug, comp)
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't create the competition: {exc}")
            return
        await ctx.send(f"🏆 Created **{name.strip()}** — "
                       "members can register with `/competition <name>`.")


async def setup(bot):
    await bot.add_cog(Competitions(bot))