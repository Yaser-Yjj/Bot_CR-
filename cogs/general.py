import discord
from discord.ext import commands

from i18n.core import resolve_member_lang, t


class HelpView(discord.ui.View):
    """Interactive help: section buttons + overview + close button.

    Sections map real cogs onto the categories people think in (fun,
    entertainment, cell management, …) rather than raw cog names, and the
    General section — hello, ping, help — is front and centre so everyday
    commands are never buried.
    """

    # (button label, emoji, cog qualified names folded into the section)
    SECTIONS = (
        ("General", "🏠", ("General",)),
        ("Fun", "🎉", ("Fun", "Engagement")),
        ("Entertainment", "🎧", ("Music",)),
        ("Cell Management", "🔬", ("Cells",)),
        ("Events & Meetings", "📅", ("Events", "Meetings")),
        ("Competitions", "🏆", ("Competitions",)),
        ("Polls", "🗳️", ("Polls",)),
        ("Minecraft", "⛏️", ("Minecraft",)),
        ("Members & Stats", "👥", ("Members", "Stats", "BirthdayTracker", "Onboarding")),
        ("Moderation & Rules", "🛡️", ("Moderation", "Rules")),
        ("Server Ops", "⚙️", ("DashBoard", "Apis", "Tasks", "Welcome", "Goodbye", "BotAdmin")),
    )

    def __init__(self, bot, *, lang: str = "en", timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.lang = lang
        self.categories: dict[str, tuple] = {}
        for cog in bot.cogs.values():
            cmds = [c for c in cog.walk_commands() if not c.hidden and c.parent is None]
            if cmds:
                self.categories[cog.qualified_name] = (cog, cmds)

        # Keep only sections whose cogs actually loaded commands.
        self.sections: dict[str, tuple[str, ...]] = {}
        for label, _emoji, cog_names in self.SECTIONS:
            present = tuple(name for name in cog_names if name in self.categories)
            if present:
                self.sections[label] = present

        # One button per section (max five per row), then home/close below.
        # Labels follow the viewer's language; section keys stay English.
        for idx, (label, emoji, _cog_names) in enumerate(self.SECTIONS):
            if label not in self.sections:
                continue
            button = discord.ui.Button(
                label=self._section_label(label), emoji=emoji,
                style=discord.ButtonStyle.secondary, row=idx // 5,
            )
            button.callback = self._section_callback(label)
            self.add_item(button)
        self.home.label = t("help.all", lang)
        self.close.label = t("help.close", lang)

    def _section_label(self, label: str) -> str:
        return t(f"help.section.{label}", self.lang)

    def _section_callback(self, label: str):
        async def callback(interaction: discord.Interaction):
            try:
                await interaction.response.edit_message(
                    embed=self.build_section_embed(label), view=self
                )
            except discord.HTTPException:
                pass
        return callback

    def _section_names(self, label: str) -> list[str]:
        """Sorted command names that belong to a section."""
        names = []
        for cog_name in self.sections[label]:
            _cog, cmds = self.categories[cog_name]
            names.extend(c.name for c in cmds)
        return sorted(names)

    def build_overview_embed(self) -> discord.Embed:
        prefix = self.bot.command_prefix
        embed = discord.Embed(
            title=t("help.title", self.lang),
            description=t("help.description", self.lang, prefix=prefix),
            color=discord.Color.blue(),
        )
        for label in self.sections:
            names = self._section_names(label)
            embed.add_field(
                name=self._section_label(label),
                value=", ".join(f"`{n}`" for n in names) or "—",
                inline=False,
            )
        embed.set_footer(
            text=t("help.footer", self.lang, n=len(self.bot.commands))
        )
        return embed

    def build_section_embed(self, label: str) -> discord.Embed:
        cog_names = self.sections[label]
        blurbs = []
        for cog_name in cog_names:
            cog, _cmds = self.categories[cog_name]
            first_line = (cog.__doc__ or "").strip().splitlines()
            if first_line:
                blurbs.append(first_line[0])
        embed = discord.Embed(
            title=t("help.section_title", self.lang,
                    section=self._section_label(label)),
            description="\n".join(blurbs) or None,
            color=discord.Color.blurple(),
        )
        lines = []
        for cog_name in cog_names:
            cog, cmds = self.categories[cog_name]
            if len(cog_names) > 1:
                lines.append(f"**{cog_name}**")
            for command in sorted(cmds, key=lambda c: c.name):
                usage = command.name
                for param in command.clean_params.values():
                    usage += f" <{param.name}>" if param.required else f" [{param.name}]"
                doc = command.description or command.short_doc or ""
                lines.append(f"`{usage}` — {doc}")
        for chunk in (lines[i:i + 12] for i in range(0, len(lines), 12)):
            embed.add_field(name="\u200b", value="\n".join(chunk), inline=False)
        return embed

    @discord.ui.button(emoji="🏠", style=discord.ButtonStyle.secondary, label="All", row=2)
    async def home(self, interaction: discord.Interaction, _button: discord.ui.Button):
        try:
            await interaction.response.edit_message(
                embed=self.build_overview_embed(), view=self
            )
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, label="Close", row=2)
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(
                content=t("help.closed", self.lang),
                embed=None,
                view=self,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class General(commands.Cog):
    """Basic utility commands that ship with the bot."""

    def __init__(self, bot):
        self.bot = bot

    # Command: hello
    @commands.hybrid_command(name="hello", description="Say hello!")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def hello(self, ctx):
        await ctx.send(f"Hello, {ctx.author.mention}!")

    # Command: ping
    @commands.hybrid_command(name="ping", description="Check the bot's heartbeat.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ping(self, ctx):
        latency = round(self.bot.latency * 1000)
        await ctx.send(f"Pong! 🏓 Latency: {latency} ms")

    # Command: help (custom, replaces the default)
    @commands.hybrid_command(name="help", description="Browse commands with an interactive menu.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def help_command(self, ctx):
        lang = await resolve_member_lang(
            ctx.author.id,
            locale=str(ctx.interaction.locale) if ctx.interaction else None,
        )
        view = HelpView(self.bot, lang=lang)
        embed = view.build_overview_embed()
        await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(General(bot))