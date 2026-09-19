"""Bot operator console — status, restart, update (bot admins only).

Handy for the club's on-call operatives: `/bot status` shows how the running
process is doing, `/bot restart` reloads the whole bot in place, and
`/bot update` pulls the latest `nightly` from GitHub and restarts — no SSH
needed. All commands are gated by :func:`cogs._perms.is_bot_admin`.

Restart strategy: the bot runs under systemd with ``NoNewPrivileges=true``, so
it can't `sudo systemctl restart` itself. Instead ``restart`` and ``update``
reply first and then ``os.execv`` back into ``BOT.py``: the same PID keeps
running (systemd never notices), every cog and the KeepAlive Flask server
re-initialise, and the whole process starts fresh — equivalent to a service
restart without any privilege escalation.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import discord
from discord.ext import commands

from cogs._perms import is_bot_admin

LOG = logging.getLogger("bot.admin")

ROOT = Path(__file__).resolve().parent.parent
_START = time.monotonic()

# Left behind before a relaunch; consumed on the next on_ready so the channel
# that triggered restart/update gets a "back online" message. Lives in the repo
# root (gitignored) because systemd's PrivateTmp keeps /tmp private per-boot.
ACK_FILE = ROOT / ".restart-ack.json"


def _write_ack(guild_id: int | None, channel_id: int | None,
               by: str | None, *, after_update: bool) -> None:
    """Persist where the current session's relaunch should report back."""
    data = {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "by": by,
        "after_update": after_update,
    }
    try:
        ACK_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not write restart ack: %s", exc)


def _fmt_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _git_head() -> str:
    """Current branch + short commit, or 'unknown' if git isn't available."""
    try:
        rev = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        branch = subprocess.run(
            ["git", "-C", str(ROOT), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return f"{branch}@{rev}" if branch and rev else "unknown"
    except Exception:  # noqa: BLE001 - status should never raise
        return "unknown"


_LAST_UPDATE_CHECK = 0.0
_UPDATE_STATE_CACHE = None


def _git_update_state(*, force: bool = False
                      ) -> tuple[int | None, int | None, str | None, str | None]:
    """How far the running HEAD is from ``origin/nightly`` (fetched).

    Returns ``(behind, ahead, remote_short, error)``. ``ahead > 0`` with
    ``behind > 0`` means the branch has diverged — ``git pull --ff-only``
    would refuse, so no update button is offered. Results are cached for
    120 s so spamming /bot status doesn't hammer GitHub.
    """
    global _LAST_UPDATE_CHECK, _UPDATE_STATE_CACHE
    now = time.monotonic()
    if not force and _UPDATE_STATE_CACHE and now - _LAST_UPDATE_CHECK < 120:
        return _UPDATE_STATE_CACHE

    def _put(state):
        global _LAST_UPDATE_CHECK, _UPDATE_STATE_CACHE
        _LAST_UPDATE_CHECK, _UPDATE_STATE_CACHE = now, state
        return state

    try:
        subprocess.run(["git", "-C", str(ROOT), "fetch", "origin"],
                       capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001 - network or git absence
        return _put((None, None, None, f"couldn't reach GitHub ({exc})"))

    def _rev(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(ROOT), *args],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
        except Exception:  # noqa: BLE001 - non-fatal for status
            return ""

    remote = _rev("rev-parse", "--short", "origin/nightly")
    if not remote:
        return _put((None, None, None, "no origin/nightly ref to compare"))
    try:
        behind = int(_rev("rev-list", "--count", "HEAD..origin/nightly") or 0)
        ahead = int(_rev("rev-list", "--count", "origin/nightly..HEAD") or 0)
    except ValueError:
        return _put((None, None, None, "couldn't count commits"))
    return _put((behind, ahead, remote, None))


def _git_update_log(n: int = 3) -> list[str]:
    """Short commit subjects that HEAD..origin/nightly would bring."""
    out = subprocess.run(
        ["git", "-C", str(ROOT), "log", "--oneline", f"-{n}", "HEAD..origin/nightly"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    return out.splitlines() if out else []


def _git_pull() -> tuple[bool, str]:
    """The exact update routine /bot update runs; (ok, summary|error)."""
    proc = subprocess.run(
        ["git", "-C", str(ROOT), "pull", "--ff-only", "origin", "nightly"],
        capture_output=True, text=True, timeout=90,
    )
    if proc.returncode != 0:
        return False, proc.stderr.strip()[:1500] or "unknown error"
    summary = (proc.stdout.strip().splitlines() or ["already up to date."])[-1]
    return True, summary


def _relaunch() -> None:
    """Replace this process with a fresh copy of BOT.py (same PID)."""
    os.execv(sys.executable, [sys.executable, str(ROOT / "BOT.py")])


class BotUpdateView(discord.ui.View):
    """Single ⬆️ button under /bot status when origin/nightly is ahead."""

    def __init__(self, behind: int, remote: str,
                 base_embed: discord.Embed, *, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.behind, self.remote, self.base_embed = behind, remote, base_embed
        self.button.label = f"Update to nightly ({behind} commits)"

    @discord.ui.button(emoji="⬆️", style=discord.ButtonStyle.primary)
    async def button(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        if interaction.guild is None or not is_bot_admin(interaction.user):
            return  # view is only shown to admins; ignore everyone else
        embed = discord.Embed(
            title="⬆️ Update available",
            description=(
                f"**nightly** is **{self.behind} commit(s)** ahead of this build "
                f"(`nightly@{self.remote}`).\n\n"
                "This runs `git pull --ff-only origin nightly` and then "
                "**restarts the bot** — the same thing as `/bot update`.\n"
                "Continue?"
            ),
            color=0x2ECC71,
        )
        await interaction.response.edit_message(
            embed=embed, view=BotUpdateConfirmView(self.behind, self.remote,
                                                   self.base_embed))


class BotUpdateConfirmView(discord.ui.View):
    """"Yes, update & restart" / "Cancel" for the status button."""

    def __init__(self, behind: int, remote: str,
                 base_embed: discord.Embed, *, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.behind, self.remote, self.base_embed = behind, remote, base_embed
        self.yes.label = "Update & restart"
        self.cancel.label = "Cancel"

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction,
                  _button: discord.ui.Button):
        if interaction.guild is None or not is_bot_admin(interaction.user):
            return
        await interaction.response.edit_message(
            content="🔄 Pulling latest nightly…", embed=None, view=None)
        ok, info = await asyncio.to_thread(_git_pull)
        if not ok:
            await interaction.message.edit(
                content=f"⚠️ Pull failed:\n```{info}```", embed=None, view=None)
            LOG.warning("Status-button update failed for %s: %s",
                        interaction.user, info)
            return
        _write_ack(interaction.guild_id, interaction.channel_id,
                   str(interaction.user), after_update=True)
        LOG.info("Update applied via status button by %s: %s",
                 interaction.user, info)
        await interaction.message.edit(
            content=f"✅ `{info}` — restarting…", embed=None, view=None)
        _relaunch()

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        view = BotUpdateView(self.behind, self.remote, self.base_embed)
        await interaction.response.edit_message(embed=self.base_embed, view=view)


class BotAdmin(commands.Cog):
    """Operator console for the bot itself (bot admins only)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @staticmethod
    async def _require_admin(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            await ctx.send("⛔ Run this inside the server.")
            return False
        if not is_bot_admin(ctx.author):
            await ctx.send("⛔ Bot admins only.")
            return False
        return True

    @commands.hybrid_group(
        name="bot",
        description="Bot operator commands (status, restart, update).",
        invoke_without_command=True,
    )
    async def bot(self, ctx: commands.Context):
        """`bot status` / `bot restart` / `bot update` — what this console does."""
        if not await self._require_admin(ctx):
            return
        await ctx.send("🛠️ `bot status` — health | `bot restart` — reload process | "
                       "`bot update` — pull latest nightly + restart")

    @commands.Cog.listener()
    async def on_ready(self):
        """Send the post-restart/post-update "back online" ack, once."""
        if not ACK_FILE.exists():
            return
        try:
            data = json.loads(ACK_FILE.read_text(encoding="utf-8"))
            ACK_FILE.unlink()
        except (OSError, ValueError) as exc:
            LOG.warning("Ignoring unreadable restart ack: %s", exc)
            ACK_FILE.unlink(missing_ok=True)
            return
        channel = self.bot.get_channel(data.get("channel_id") or 0)
        if channel is None:
            return
        head = await asyncio.to_thread(_git_head)
        by = data.get("by")
        verb = "Update applied — back online." if data.get("after_update") else "Back online."
        text = f"✅ {verb}"
        if by:
            text += f" ({by})"
        text += f" — build `{head}` ✅"
        try:
            await channel.send(text)
        except discord.HTTPException as exc:
            LOG.warning("Could not send restart ack: %s", exc)

    @bot.command(name="status", description="Show bot uptime, build, and command counts.")
    @commands.guild_only()
    async def status(self, ctx: commands.Context):
        if not await self._require_admin(ctx):
            return
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        head = await asyncio.to_thread(_git_head)
        voice = len([v for v in self.bot.voice_clients if v.is_connected()])
        embed = discord.Embed(title="🤖 Bot status", color=0x2ECC71)
        embed.add_field(name="Uptime",
                        value=_fmt_uptime(int(time.monotonic() - _START)))
        embed.add_field(name="Latency",
                        value=f"{self.bot.latency * 1000:.0f} ms")
        embed.add_field(name="Build", value=f"`{head}`")
        embed.add_field(name="Cogs", value=str(len(self.bot.cogs)))
        embed.add_field(name="Commands", value=str(len(self.bot.commands)))
        embed.add_field(name="Slash", value=str(len(self.bot.tree.get_commands())))
        embed.add_field(name="Voice connections", value=str(voice))
        behind, ahead, remote, check_err = await asyncio.to_thread(
            _git_update_state)
        if check_err:
            embed.add_field(name="Update", value=f"❓ {check_err}")
            await ctx.send(embed=embed)
            return
        if behind is None:
            embed.add_field(name="Update", value="❓ Could not check updates.")
            await ctx.send(embed=embed)
            return
        if behind == 0:
            if ahead:
                embed.add_field(name="Update",
                                value=f"🧭 Running {ahead} commit(s) ahead of "
                                      f"`origin/nightly` — no update needed.")
            else:
                embed.add_field(name="Update",
                                value=f"✅ Up to date with `nightly@{remote}`.")
            await ctx.send(embed=embed)
            return
        if ahead:
            embed.add_field(
                name="Update",
                value=f"⚠️ **Diverged** from `origin/nightly` — a fast-forward "
                      f"update isn't possible; rebuild/deploy manually.")
            await ctx.send(embed=embed)
            return
        preview = "\n".join(
            f"`{line}`" for line in await asyncio.to_thread(_git_update_log, 3))
        embed.add_field(
            name="Update",
            value=f"⬆️ **{behind} commit(s) behind** `nightly@{remote}`\n"
                  f"New in nightly:\n{preview}",
            inline=False,
        )
        await ctx.send(embed=embed, view=BotUpdateView(behind, remote, embed))

    @bot.command(name="restart", description="Restart the bot process (reloads all cogs).")
    @commands.guild_only()
    async def restart(self, ctx: commands.Context):
        if not await self._require_admin(ctx):
            return
        await ctx.send("🔄 Restarting… I'll be back in a few seconds.")
        LOG.info("Restart requested by %s (%s)", ctx.author, ctx.guild)
        _write_ack(ctx.guild.id, ctx.channel.id, str(ctx.author), after_update=False)
        _relaunch()

    @bot.command(name="update", description="Pull the latest nightly from GitHub and restart.")
    @commands.guild_only()
    async def update(self, ctx: commands.Context):
        if not await self._require_admin(ctx):
            return
        await ctx.defer()
        ok, info = await asyncio.to_thread(_git_pull)
        if not ok:
            await ctx.send(f"⚠️ Pull failed:\n```{info}```")
            return
        await ctx.send(f"✅ `{info}` — restarting…")
        LOG.info("Update applied by %s: %s", ctx.author, info)
        _write_ack(ctx.guild.id, ctx.channel.id, str(ctx.author), after_update=True)
        _relaunch()


async def setup(bot: commands.Bot):
    await bot.add_cog(BotAdmin(bot))