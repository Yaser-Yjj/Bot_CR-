"""Interactive settings — language picker with button drill-down menus.

The ``/settings`` command opens a root menu whose *content* is the current
state and whose *buttons* navigate deeper — Dank Memer style — until the
actual choice is tapped:

    /settings ──▶ 🌐 Language ──▶ 🇬🇧 English · 🇫🇷 Français · 🇸🇦 العربية

The chosen language is stored per member (store ``lang`` field), so every
interactive command then answers that member in their language. Server
broadcasts (welcome channel, dashboard) remain English by design.
"""

import logging
from typing import Literal

import discord
from discord.ext import commands

from data.store import store
from i18n.core import LANGUAGES, resolve_member_lang, t
from cogs._ui import OwnerView, LoggedView, close_panel

LOG = logging.getLogger("bot.settings")

_LANGUAGE_META = (("en", "🇬🇧"), ("fr", "🇫🇷"), ("ar", "🇸🇦"))


class SettingsView(LoggedView, OwnerView, discord.ui.View):
    """Root settings menu: 🌐 Language picker + ✖️ Close."""

    def __init__(self, lang: str, user_id: int, *, timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.lang = lang
        self.user_id = user_id
        self.close.label = t("settings.close", lang)
        picker = discord.ui.Select(
            placeholder=t("settings.lang_button", lang),
            options=[discord.SelectOption(
                value="language", emoji="🌐",
                label=t("settings.lang_button", lang))],
            row=0,
        )
        picker.callback = self._open_language
        self.add_item(picker)

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This settings panel belongs to someone else — "
                "run `/settings` yourself to change your own.")

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=t("settings.title", self.lang),
            description=t("settings.description", self.lang),
            color=discord.Color.blurple(),
        )

    async def _open_language(self, interaction: discord.Interaction):
        if not await self._owned(interaction):
            return
        view = LanguageView(self.lang, self.user_id)
        try:
            await interaction.response.edit_message(embed=view.embed(), view=view)
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, label="Close", row=1)
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        await close_panel(interaction, text=t("settings.closed", self.lang))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class LanguageView(LoggedView, OwnerView, discord.ui.View):
    """One level deeper: pick a language, or ◀️ back to the root menu."""

    def __init__(self, lang: str, user_id: int, *, timeout: float = 120.0):
        super().__init__(timeout=timeout)
        self.lang = lang
        self.user_id = user_id
        picker = discord.ui.Select(
            placeholder=t("settings.lang_pick", lang),
            options=[discord.SelectOption(
                emoji=emoji, label=LANGUAGES[code], value=code)
                for code, emoji in _LANGUAGE_META],
            row=0,
        )
        picker.callback = self._picked
        self.add_item(picker)
        back = discord.ui.Button(
            emoji="◀️", label=t("settings.back", lang),
            style=discord.ButtonStyle.secondary, row=1,
        )
        back.callback = self._go_back
        self.add_item(back)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=t("settings.lang_title", self.lang),
            description=t("settings.lang_description", self.lang),
            color=discord.Color.blurple(),
        )

    async def _picked(self, interaction: discord.Interaction):
        """A language was chosen from the dropdown — never for a stranger."""
        if not await self._owned(interaction):
            return
        await self._apply(interaction, interaction.values[0])

    async def _apply(self, interaction: discord.Interaction, code: str):
        # Never let a stranger rewrite your stored preference by tapping your
        # language buttons (S3: cross-user write).
        if not await self._owned(interaction):
            return
        try:
            await store.merge_member(self.user_id, {"lang": code})
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("could not save lang %s for %s: %s", code, self.user_id, exc)
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(
            title=t("settings.lang_set_title", code),
            description=t("settings.lang_set", code, name=LANGUAGES[code]),
            color=discord.Color.green(),
        )
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def _go_back(self, interaction: discord.Interaction):
        if not await self._owned(interaction):
            return
        view = SettingsView(self.lang, self.user_id)
        try:
            await interaction.response.edit_message(embed=view.embed(), view=view)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class Settings(commands.Cog):
    """Interactive settings menu (language) with button drill-down."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    async def _lang(ctx: commands.Context) -> str:
        locale = str(ctx.interaction.locale) if ctx.interaction else None
        return await resolve_member_lang(ctx.author.id, locale)

    @commands.hybrid_command(name="settings",
                             description="Open the interactive settings menu (language, …).")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def settings(self, ctx: commands.Context):
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        lang = await self._lang(ctx)
        view = SettingsView(lang, ctx.author.id)
        await ctx.send(embed=view.embed(), view=view, ephemeral=True)

    @commands.hybrid_command(name="language",
                             description="Set the language the bot answers you in.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def language(self, ctx: commands.Context,
                       choice: Literal["english", "french", "arabic"]):
        """Set your bot language: english, french or arabic."""
        code = {"english": "en", "french": "fr", "arabic": "ar"}[choice]
        try:
            await store.merge_member(ctx.author.id, {"lang": code})
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("could not save lang for %s: %s", ctx.author.id, exc)
        await ctx.send(t("settings.lang_set", code, name=LANGUAGES[code]),
                       ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Settings(bot))