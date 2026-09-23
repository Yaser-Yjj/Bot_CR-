"""Reusable interactive Discord components for Bot_CR.

This file is NOT a cog: the underscore prefix keeps the auto-loader in
BOT.py (and scripts/smoke_test.py) from treating it as an extension. Cog
modules import these views with ``from cogs._ui import ...``.
"""

import asyncio
import logging

import discord

LOG = logging.getLogger("bot.ui")

__all__ = ["ConfirmView", "PaginatorView", "OwnerView",
           "LoggedView", "close_panel"]


class OwnerView:
    """Shared author-scoping for interactive views.

    Subclasses set ``self.user_id`` (int) on construction; ``owned`` refuses
    button presses from anyone else with an ephemeral notice. ``user_id=None``
    leaves the view open to everyone (shared panels such as polls). Override
    ``_owner_deny_message`` to tailor the refusal text.
    """

    async def owned(self, interaction: discord.Interaction) -> bool:
        user_id = getattr(self, "user_id", None)
        if user_id is None or interaction.user.id == user_id:
            return True
        await interaction.response.send_message(
            self._owner_deny_message(interaction), ephemeral=True)
        return False

    # Alias used by view callbacks; kept so refactors stay mechanical.
    async def _owned(self, interaction: discord.Interaction) -> bool:
        return await self.owned(interaction)

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This view belongs to the command author — run the "
                "command yourself to interact with it.")


class ConfirmView(OwnerView, discord.ui.View):
    """Two-button confirm/cancel prompt for destructive actions.

    ``on_confirm`` must be an async callable taking the button interaction.
    Only the member who opened the prompt may confirm or cancel it; strangers
    get an ephemeral refusal and the buttons stay live for the owner. The
    buttons disable after the authorized press so the action can't repeat.
    """

    def __init__(self, on_confirm, *, user=None, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.on_confirm = on_confirm
        self.user_id = user.id if hasattr(user, "id") else user

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return "🔒 This prompt belongs to the command author — you can't act on it."

    @discord.ui.button(style=discord.ButtonStyle.danger, label="Confirm")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        # The action callback sends its own followup on this interaction.
        await self.on_confirm(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Cancel")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❌ Cancelled.", embed=None, view=None
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class LoggedView:
    """Mixin: surface view interaction errors in the service logs.

    Without this, an exception inside a button/select callback makes Discord
    show a generic "interaction failed" with no trace anywhere — adding it to
    a view converts those silent failures into ``bot.ui`` log lines.
    """

    async def on_error(self, interaction: discord.Interaction, error: Exception,
                       item) -> None:
        LOG.error("%s interaction error (item=%s, user=%s): %s",
                  type(self).__name__, type(item).__name__,
                  getattr(getattr(interaction, "user", None), "id", "?"),
                  error)


async def close_panel(interaction: discord.Interaction, *, text: str,
                      delay: float = 5.0) -> None:
    """✖️ close: swap the panel for ``text``, then delete it after ``delay`` s.

    Discord's original-response delete works even for ephemeral panels
    (where ``interaction.message`` is ``None``); the ``interaction.message``
    fallback covers prefix-invoked panels that have no interaction response.
    """
    try:
        await interaction.response.edit_message(content=text, embed=None, view=None)
    except (discord.HTTPException, discord.InteractionResponded):
        return
    if delay > 0:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
    try:
        await interaction.delete_original_response()
    except (discord.HTTPException, AttributeError):
        try:
            await interaction.message.delete()
        except (discord.HTTPException, AttributeError):
            pass


class PaginatorView(OwnerView, discord.ui.View):
    """◀ ▶ pager over a list of pages (strings and/or embeds).

    Pass ``user`` to scope the pager to its commanding member; without it the
    pager stays open to everyone (legacy callers).
    """

    def __init__(self, pages: list, *, timeout: float = 180.0, start: int = 0,
                 user: discord.Member = None):
        super().__init__(timeout=timeout)
        if not pages:
            raise ValueError("PaginatorView needs at least one page")
        self.pages = pages
        self.user_id = user.id if user is not None else None
        self._index = max(0, min(start, len(pages) - 1))
        self.page_label.label = f"{self._index + 1}/{len(self.pages)}"
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev.disabled = self._index == 0
        self.next.disabled = self._index == len(self.pages) - 1
        self.page_label.label = f"{self._index + 1}/{len(self.pages)}"

    def _owner_deny_message(self, _interaction: discord.Interaction) -> str:
        return ("🔒 This pager belongs to the command author — run the command "
                "yourself to page through it.")

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self._index = max(0, self._index - 1)
        await self._update(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="1/1", disabled=True)
    async def page_label(self, _interaction: discord.Interaction, _button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._owned(interaction):
            return
        self._index = min(len(self.pages) - 1, self._index + 1)
        await self._update(interaction)

    async def _update(self, interaction: discord.Interaction):
        self._sync_buttons()
        page = self.pages[self._index]
        kwargs: dict = {"view": self, "content": None, "embed": None}
        if isinstance(page, discord.Embed):
            kwargs["embed"] = page
        else:
            kwargs["content"] = page
        try:
            await interaction.response.edit_message(**kwargs)
        except discord.InteractionResponded:
            await interaction.followup.edit_message(interaction.message.id, **kwargs)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True