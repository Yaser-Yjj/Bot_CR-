import logging
import math
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import config
from data.store import store
from data.store import StoreError
from cogs._ui import PaginatorView

LOG = logging.getLogger("bot.engagement")

tz = ZoneInfo("Africa/Casablanca")


def level_from_xp(xp: int) -> int:
    """Level from cumulative XP. Level L needs 100*L*L total XP."""
    return int(math.isqrt(max(0, int(xp)) // 100))


def xp_for_level(level: int) -> int:
    return 100 * level * level


def xp_progress(xp: int) -> tuple[int, int, int]:
    """Return (level, xp into level, xp needed for next level)."""
    level = level_from_xp(xp)
    into = xp - xp_for_level(level)
    need = xp_for_level(level + 1) - xp_for_level(level)
    return level, into, need


def today_str() -> str:
    return datetime.now(tz).strftime("%Y-%m-%d")


class Engagement(commands.Cog):
    """XP & levels plus daily challenges, all Appwrite-backed."""

    def __init__(self, bot):
        self.bot = bot
        self.xp_pending = {}       # user_id -> XP earned since last flush
        self.xp_base = {}          # user_id -> stored XP as of last read
        self.level_seen = {}       # user_id -> highest announced level
        self.name_cache = {}       # user_id -> username
        self.xp_cooldown = {}      # user_id -> last time XP was granted
        self.daily_posted = None   # last date the challenge was posted for
        self.flush_loop.start()

    # ── XP accumulation ────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None:
            return
        uid = message.author.id
        self.name_cache[uid] = message.author.name

        now = time.monotonic()
        if now - self.xp_cooldown.get(uid, 0.0) < config.XP_COOLDOWN_SECONDS:
            return
        self.xp_cooldown[uid] = now

        gain = random.randint(config.XP_MIN, config.XP_MAX)
        self.xp_pending[uid] = self.xp_pending.get(uid, 0) + gain

        base = self.xp_base.get(uid)
        if base is None:
            try:
                record = await store.get_member(uid)
            except StoreError:
                record = None
            base = int((record or {}).get("xp", 0))
            self.xp_base[uid] = base

        total = base + self.xp_pending[uid]
        level = level_from_xp(total)
        prev = self.level_seen.get(uid, level_from_xp(base))
        if level > prev:
            self.level_seen[uid] = level
            await message.channel.send(
                f"🎉 {message.author.mention} reached **level {level}**! GG!"
            )

    # ── flush ──────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        if not self.flush_loop.is_running():
            self.flush_loop.start()

    @tasks.loop(seconds=config.DASHBOARD_REFRESH_SECONDS)
    async def flush_loop(self):
        await self.flush()
        await self.maybe_post_daily_challenge()

    async def flush(self):
        """Persist pending XP deltas and update the local XP bases."""
        deltas = {}
        for uid, amount in self.xp_pending.items():
            deltas[uid] = {
                "xp": amount,
                "_bootstrap": {"username": self.name_cache.get(uid, "")},
            }
        if not deltas:
            return
        try:
            await store.flush_member_activity(deltas)
        except StoreError as exc:
            LOG.error("XP flush failed: %s", exc)
            return
        for uid, amount in self.xp_pending.items():
            self.xp_base[uid] = self.xp_base.get(uid, 0) + amount
        self.xp_pending.clear()

    # ── daily challenge auto-post ──────────────────────────────
    async def maybe_post_daily_challenge(self):
        """Post today's challenge to the announcements channel (once per day)."""
        date = today_str()
        if self.daily_posted == date:
            return
        self.daily_posted = date
        try:
            challenge = await store.get_challenge(date)
        except StoreError:
            return
        if not challenge:
            return
        channel = discord.utils.get(
            self.bot.guilds[0].text_channels, name=config.CHANNEL_ANNOUNCEMENTS
        ) if self.bot.guilds else None
        if channel is None:
            return
        await channel.send(
            f"🔥 **Today's challenge ({date}):** {challenge.get('title', '')}"
            + (f"\n{challenge.get('description', '')}" if challenge.get("description") else "")
        )

    # ── XP commands ────────────────────────────────────────────
    @commands.hybrid_command(name="rank", description="Show your (or someone's) level and XP.")
    async def rank(self, ctx, member: discord.Member | None = None):
        member = member or ctx.author
        uid = member.id
        try:
            record = await store.get_member(uid)
        except StoreError:
            record = None
        xp = int((record or {}).get("xp", 0)) + self.xp_pending.get(uid, 0)
        level, into, need = xp_progress(xp)
        filled = int(10 * into / need) if need else 10
        bar = "🟩" * filled + "⬛" * (10 - filled)
        embed = discord.Embed(
            title=f"📈 {member.display_name} — Level {level}",
            color=discord.Color.green(),
        )
        embed.add_field(name="XP", value=f"{xp} total ({into}/{need} to next level)")
        embed.add_field(name="Progress", value=f"{bar}", inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="leaderboard", description="Server XP leaderboard (paginated).")
    @commands.cooldown(1, 10, commands.BucketType.channel)
    async def leaderboard(self, ctx):
        try:
            members = await store.list_members(limit=100, order_by="xp")
        except StoreError:
            members = []
        rows = []
        for i, record in enumerate(members, start=1):
            uid = int(record.get("user_id") or 0)
            xp = int(record.get("xp", 0)) + self.xp_pending.get(uid, 0)
            name = (
                record.get("display_name")
                or record.get("username")
                or (self.bot.get_user(uid).name if self.bot.get_user(uid) else "???")
            )
            rows.append(f"`{i:>2}.` **{name}** — level {level_from_xp(xp)} ({xp} XP)")
        if not rows:
            await ctx.send("🏆 No XP recorded yet — start chatting!")
            return
        page_size = 10
        pages = []
        total_pages = (len(rows) + page_size - 1) // page_size
        for p in range(total_pages):
            embed = discord.Embed(
                title="🏆 Leaderboard",
                description="\n".join(rows[p * page_size:(p + 1) * page_size]),
                color=discord.Color.gold(),
            )
            embed.set_footer(text=f"Page {p + 1}/{total_pages}")
            pages.append(embed)
        if len(pages) == 1:
            await ctx.send(embed=pages[0])
        else:
            await ctx.send(embed=pages[0], view=PaginatorView(pages))

    # ── daily challenge commands ───────────────────────────────
    @commands.hybrid_group(name="challenge", description="Daily challenges.")
    async def challenge(self, ctx):
        await self.challenge_today(ctx)

    @challenge.command(name="today", description="Show today's challenge.")
    async def challenge_today(self, ctx):
        try:
            record = await store.get_challenge(today_str())
        except StoreError:
            record = None
        if not record:
            await ctx.send("📭 No challenge set for today yet.")
            return
        claimed = record.get("claimed") or []
        names = []
        for cid in claimed[:10]:
            member = ctx.guild.get_member(int(cid))
            names.append(member.display_name if member else f"<@{cid}>")
        message = f"🔥 **Today's challenge:** {record.get('title', '')}"
        if record.get("description"):
            message += f"\n{record['description']}"
        message += f"\n✅ Claimed by: {', '.join(names) if names else 'no one yet'}"
        await ctx.send(message)

    @challenge.command(name="set", description="Set today's challenge (staff).")
    @commands.has_permissions(manage_messages=True)
    async def challenge_set(self, ctx, title: str, description: str = ""):
        await store.save_challenge(
            today_str(),
            title=title,
            description=description,
            created_by=ctx.author.display_name,
        )
        await ctx.send(f"✅ Today's challenge set: **{title}**")

    @challenge.command(name="claim", description="Claim today's challenge (once).")
    async def challenge_claim(self, ctx):
        try:
            claimed = await store.claim_challenge(today_str(), ctx.author.id)
        except StoreError as exc:
            await ctx.send(f"⚠️ Could not claim: {exc}")
            return
        await ctx.send(
            "🎉 Challenge claimed! Nice work."
            if claimed
            else "⚠️ You already claimed today's challenge!"
        )

    @challenge.command(name="history", description="The last few challenges.")
    async def challenge_history(self, ctx):
        try:
            recent = await store.list_recent_challenges(limit=7)
        except StoreError:
            recent = []
        if not recent:
            await ctx.send("No challenges recorded yet.")
            return
        lines = [
            f"**{r.get('date', '?')}** — {r.get('title', '')}"
            f" (claimed by {len(r.get('claimed') or [])})"
            for r in recent
        ]
        await ctx.send("🗓️ **Recent challenges**\n" + "\n".join(lines))


async def setup(bot):
    await bot.add_cog(Engagement(bot))