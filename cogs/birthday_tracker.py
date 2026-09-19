import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dateutil import parser

import config
from data.store import store
from data.store import StoreError

LOG = logging.getLogger("bot.birthdays")

# Server timezone (e.g., Morocco).
tz = ZoneInfo("Africa/Casablanca")


def parse_birthday(raw: str) -> tuple[str, str]:
    """Parse a birthday string -> (full "YYYY-MM-DD", "MM-DD").

    Accepts "YYYY-MM-DD" first, then other dateutil-recognisable formats
    (both day-first and month-first). Rejects future or implausible dates.
    """
    value = raw.strip()
    if not value:
        raise ValueError("empty date")

    def _normalise(d: datetime) -> tuple[str, str]:
        if d.date() > date.today():
            raise ValueError("birthday cannot be in the future")
        if d.year < 1900:
            raise ValueError("birthday year is implausible")
        return d.strftime("%Y-%m-%d"), d.strftime("%m-%d")

    try:
        return _normalise(datetime.strptime(value, "%Y-%m-%d"))
    except ValueError:
        pass

    for dayfirst in (False, True):
        try:
            return _normalise(parser.parse(value, dayfirst=dayfirst))
        except (ValueError, OverflowError, TypeError):
            continue
    raise ValueError(f"not a date: {raw}")


async def announce_birthday(guild: discord.Guild, name: str) -> None:
    """Post a birthday announcement to the announcements channel."""
    channel = discord.utils.get(guild.text_channels, name=config.CHANNEL_ANNOUNCEMENTS)
    if channel is None:
        LOG.warning("No announcement channel %r found", config.CHANNEL_ANNOUNCEMENTS)
        return
    await channel.send(f"🎉 Happy Birthday to {name}! 🎂🎈")


class BirthdayTracker(commands.Cog):
    """Daily birthday announcements, backed by the Appwrite members store."""

    def __init__(self, bot):
        self.bot = bot
        self.check_birthdays.start()

    @commands.Cog.listener()
    async def on_ready(self):
        # Reconnect guard: the loop may already be running.
        if not self.check_birthdays.is_running():
            self.check_birthdays.start()

    @tasks.loop(hours=24)
    async def check_birthdays(self):
        await self.bot.wait_until_ready()
        today = datetime.now(tz).strftime("%m-%d")

        guild = self.bot.guilds[0] if self.bot.guilds else None
        if guild is None:
            return
        channel = discord.utils.get(guild.text_channels, name=config.CHANNEL_ANNOUNCEMENTS)
        if channel is None:
            LOG.warning("No announcement channel %r found", config.CHANNEL_ANNOUNCEMENTS)
            return

        try:
            members = await store.list_members(limit=100, order_by="messages")
        except StoreError:
            LOG.error("Could not read members for the birthday check")
            return

        for rec in members:
            if rec.get("birthday") != today:
                continue
            name = (
                rec.get("display_name")
                or rec.get("real_name")
                or rec.get("username")
                or "??? "
            )
            LOG.info("Sending birthday announcement for %s", name)
            await channel.send(f"🎉 Happy Birthday to {name}! 🎂🎈")


async def setup(bot):
    await bot.add_cog(BirthdayTracker(bot))