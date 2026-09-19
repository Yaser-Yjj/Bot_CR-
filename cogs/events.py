"""Club events — RSVPs stored in Appwrite (single source of truth).

``/event create`` (chiefs+) posts an event; ``/events`` lists upcoming ones;
``/event <title>`` shows details with ✅/❌/❔ buttons whose answers are
written back to the bot_events collection, so the website sees the same
attendance numbers.
"""

import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from data.store import store
from data.store import StoreError
from cogs._dates import days_until, fmt_date, slugify
from cogs._scopes import require_scope

LOG = logging.getLogger("bot.events")


def _event_embed(event: dict) -> discord.Embed:
    attendees = event.get("attendees") or []
    declined = event.get("declined") or []
    embed = discord.Embed(
        title=f"📅 {event.get('title', '?')}",
        description=event.get("description") or "",
        color=discord.Color.blue(),
    )
    embed.add_field(name="Date", value=fmt_date(event.get("date")), inline=True)
    embed.add_field(name="Time", value=event.get("time") or "—", inline=True)
    embed.add_field(name="📍 Location", value=event.get("location") or "TBD", inline=True)
    embed.add_field(name="✅ Attending", value=f"{len(attendees)}", inline=True)
    embed.add_field(name="❌ Can't make it", value=f"{len(declined)}", inline=True)
    return embed


class EventView(discord.ui.View):
    """RSVP buttons — answers land in Appwrite."""

    def __init__(self, cog, slug: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.slug = slug

    @discord.ui.button(label="✅ I'm attending", style=discord.ButtonStyle.success,
                       custom_id="event:attending")
    async def attending(self, interaction, button):
        await self._rsvp(interaction, True)

    @discord.ui.button(label="❌ Can't attend", style=discord.ButtonStyle.danger,
                       custom_id="event:absent")
    async def absent(self, interaction, button):
        await self._rsvp(interaction, False)

    @discord.ui.button(label="❔ Undo", style=discord.ButtonStyle.secondary,
                       custom_id="event:undo")
    async def undo(self, interaction, button):
        await self._rsvp(interaction, None)

    @discord.ui.button(label="👥 Attendance", style=discord.ButtonStyle.secondary,
                       custom_id="event:attendance")
    async def attendance(self, interaction, button):
        event = await self.cog._get(self.slug)
        if event is None:
            await interaction.response.send_message("⚠️ Event not found.",
                                                    ephemeral=True)
            return
        attends = event.get("attendees") or []
        declines = event.get("declined") or []
        lines = [f"**{event.get('title')}**",
                 f"✅ Attending ({len(attends)}):",
                 " ".join(self.cog._name_of(interaction.guild, m) or f"<@{m}>"
                          for m in attends[:40]) or "—",
                 f"\n❌ Can't make it ({len(declines)}):",
                 " ".join(self.cog._name_of(interaction.guild, m) or f"<@{m}>"
                          for m in declines[:40]) or "—"]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    async def _rsvp(self, interaction, attending):
        try:
            label = await store.set_rsvp(self.slug, interaction.user.id, attending)
        except StoreError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        await interaction.response.edit_message(
            embed=_event_embed((await self.cog._get(self.slug))), view=self)
        await interaction.followup.send(f"{label} · **{await self.cog._display_name(self.slug)}**.",
                                        ephemeral=True)


class Events(commands.Cog):
    """Club calendar: create events, RSVP, attendance lists."""

    def __init__(self, bot):
        self.bot = bot

    async def _get(self, slug: str):
        try:
            return await store.get_event(slug)
        except StoreError:
            return None

    async def _display_name(self, slug: str) -> str:
        doc = await self._get(slug)
        return (doc or {}).get("title", slug.replace("_", " ").title())

    @staticmethod
    def _name_of(guild, user_id):
        member = guild.get_member(int(user_id)) if guild else None
        return member.display_name if member else None

    # ── commands ───────────────────────────────────────────────
    @commands.hybrid_command(name="events", description="Upcoming club events.")
    @commands.guild_only()
    @require_scope("events.read")
    async def events(self, ctx):
        """Everyone: what's coming up."""
        try:
            events = await store.list_events()
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't load events: {exc}")
            return
        upcoming = [e for e in events
                    if days_until(e.get("date")) is None or days_until(e.get("date")) >= 0]
        upcoming.sort(key=lambda e: str(e.get("date") or "9999"))
        if not upcoming:
            await ctx.send("📅 No upcoming events. Staff: `/event create`.")
            return
        lines = []
        for e in upcoming[:12]:
            att = len(e.get("attendees") or [])
            dec = len(e.get("declined") or [])
            lines.append(f"📅 **{e.get('title')}** · {fmt_date(e.get('date'))} "
                         f"{e.get('time') or ''} · 📍 {e.get('location') or 'TBD'}\n"
                         f"   ✅ {att} · ❌ {dec} · `/event {e.get('title')}`")
        await ctx.send("📅 **Upcoming events**\n\n" + "\n".join(lines))

    @commands.hybrid_group(name="event", description="Event details and RSVP.",
                           invoke_without_command=True, fallback="view")
    @commands.guild_only()
    @require_scope("events.read")
    async def event(self, ctx, title: str):
        """Details + attendance buttons (answers stored in Appwrite)."""
        slug = slugify(title)
        event = await self._get(slug)
        if event is None:
            try:
                for candidate in await store.list_events():
                    if slugify(candidate.get("title", "")) == slug or \
                       slug in slugify(candidate.get("title", "")) or \
                       slugify(candidate.get("title", "")) in slug:
                        event = candidate
                        break
            except StoreError:
                pass
        if event is None:
            await ctx.send(f"⚠️ No event matching **{title}**. Try `/events`.")
            return
        await ctx.send(embed=_event_embed(event),
                       view=EventView(self, slugify(event["title"])))

    @event.command(name="create", description="Create an event (chiefs +).")
    @commands.guild_only()
    @require_scope("events.create")
    async def event_create(self, ctx, title: str, date: str = "",
                           time: str = "", location: str = "",
                           description: str = ""):
        """Schedule a meeting / event for the club."""
        slug = slugify(title)
        if await self._get(slug) is not None:
            await ctx.send(f"⚠️ An event named **{title}** already exists.")
            return
        event = {
            "title": title.strip(),
            "date": date.strip(),
            "time": time.strip(),
            "location": location.strip(),
            "description": description.strip(),
            "attendees": [],
            "declined": [],
            "created_by": str(ctx.author.id),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await store.save_event(slug, event)
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't create the event: {exc}")
            return
        await ctx.send(f"📅 Created **{title.strip()}** — members can RSVP with "
                       f"`/event {title.strip()}`.")


async def setup(bot):
    await bot.add_cog(Events(bot))