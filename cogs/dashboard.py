import logging

import discord
from discord.ext import commands, tasks

import config
from data.store import store
from cogs.stats import ACTIVE_STATUSES, fmt_seconds

LOG = logging.getLogger("bot.dashboard")


class DashboardRefreshView(discord.ui.View):
    """'Refresh now' button attached to the dashboard embed.

    Persistent (``timeout=None`` + fixed custom_id) so it keeps working on the
    posted message after a bot restart via ``bot.add_view``.
    """

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🔄 Refresh now", style=discord.ButtonStyle.primary,
                       custom_id="dashboard_refresh")
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button):
        try:
            embed = await self.cog.create_dashboard_embed()
            await interaction.response.edit_message(embed=embed)
        except discord.HTTPException:
            pass


class DashBoard(commands.Cog):
    """Live server stats embed that reads persisted counters.

    The old implementation scanned every channel's full history every 30s
    (a rate-limit landmine) and kept its own duplicate voice tracker. Now the
    stats cog owns tracking and flushes incrementally; this cog only renders.
    """

    def __init__(self, bot):
        self.bot = bot
        self.dashboard_message = None
        self.dashboard_view = None

    @commands.Cog.listener()
    async def on_ready(self):
        await self.bot.wait_until_ready()
        await self.setup_dashboard()
        # Reconnect guard: the loop may already be running across reconnects.
        if not self.update_server_stats.is_running():
            self.update_server_stats.start()

    async def setup_dashboard(self):
        """Locate (or recreate) the bot's dashboard message in the dashboard channel."""
        for guild in self.bot.guilds:
            channel = discord.utils.get(guild.text_channels, name=config.CHANNEL_DASHBOARD)
            if channel is None:
                continue
            self.dashboard_message = None
            async for msg in channel.history(limit=10):
                if msg.author == self.bot.user:
                    self.dashboard_message = msg
                    break
            # Re-register the persistent refresh button so it works after restarts.
            self.dashboard_view = DashboardRefreshView(self)
            self.bot.add_view(self.dashboard_view)
            embed = await self.create_dashboard_embed()
            if self.dashboard_message is None:
                self.dashboard_message = await channel.send(embed=embed, view=self.dashboard_view)
            else:
                await self.dashboard_message.edit(embed=embed, view=self.dashboard_view)
            return

    async def update_dashboard(self):
        """Refresh the dashboard message with the latest persisted stats."""
        if self.dashboard_message is None:
            await self.setup_dashboard()
            return
        embed = await self.create_dashboard_embed()
        try:
            await self.dashboard_message.edit(embed=embed, view=self.dashboard_view)
        except discord.NotFound:
            self.dashboard_message = None
            await self.setup_dashboard()

    async def create_dashboard_embed(self):
        """Create an embed with the latest stats (no channel-history scans)."""
        guild = self.bot.guilds[0] if self.bot.guilds else None
        total_members = guild.member_count if guild else 0
        online_members = (
            sum(1 for m in guild.members if m.status in ACTIVE_STATUSES) if guild else 0
        )

        counters = await store.get_counters()
        total_messages = int(counters.get("total_messages", 0))
        stats_cog = self.bot.get_cog("Stats")
        if stats_cog is not None:
            total_voice_time = fmt_seconds(await stats_cog.view_total_voice_seconds())
        else:
            total_voice_time = fmt_seconds(float(counters.get("total_voice_seconds", 0.0)))

        embed = discord.Embed(title="📊  Server Dashboard", color=discord.Color.blue())
        embed.add_field(
            name="",
            value=(
                "```🟢  Online Members   : {}\n"
                "👥  Total Members    : {}\n"
                "💬  Total Messages   : {}\n"
                "🔊  Total Voice Time : {}\n```"
            ).format(online_members, total_members, total_messages, total_voice_time),
            inline=True,
        )
        embed.set_footer(text=f"Updated every {config.DASHBOARD_REFRESH_SECONDS} seconds")

        return embed

    @tasks.loop(seconds=config.DASHBOARD_REFRESH_SECONDS)
    async def update_server_stats(self):
        await self.update_dashboard()


async def setup(bot):
    await bot.add_cog(DashBoard(bot))