"""Reusable interactive Discord components for Bot_CR.

This file is NOT a cog: the underscore prefix keeps the auto-loader in
BOT.py (and scripts/smoke_test.py) from treating it as an extension. Cog
modules import these views with ``from cogs._ui import ...``.
"""

import discord

__all__ = ["ConfirmView", "PaginatorView"]


class ConfirmView(discord.ui.View):
    """Two-button confirm/cancel prompt for destructive actions.

    ``on_confirm`` must be an async callable taking the button interaction.
    The buttons disable after the first press so the action can't repeat.
    """

    def __init__(self, on_confirm, *, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.on_confirm = on_confirm

    @discord.ui.button(style=discord.ButtonStyle.danger, label="Confirm")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        # The action callback sends its own followup on this interaction.
        await self.on_confirm(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Cancel")
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="❌ Cancelled.", embed=None, view=None
        )

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class PaginatorView(discord.ui.View):
    """◀ ▶ pager over a list of pages (strings and/or embeds)."""

    def __init__(self, pages: list, *, timeout: float = 180.0, start: int = 0):
        super().__init__(timeout=timeout)
        if not pages:
            raise ValueError("PaginatorView needs at least one page")
        self.pages = pages
        self._index = max(0, min(start, len(pages) - 1))
        self.page_label.label = f"{self._index + 1}/{len(self.pages)}"
        self._sync_buttons()

    def _sync_buttons(self):
        self.prev.disabled = self._index == 0
        self.next.disabled = self._index == len(self.pages) - 1
        self.page_label.label = f"{self._index + 1}/{len(self.pages)}"

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self._index = max(0, self._index - 1)
        await self._update(interaction)

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="1/1", disabled=True)
    async def page_label(self, _interaction: discord.Interaction, _button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
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