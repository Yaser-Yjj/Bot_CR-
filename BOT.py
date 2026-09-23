import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

import config
from data.store import store
from KeepAlive import keep_alive

LOG = logging.getLogger("bot")

keep_alive()

# Create a bot instance
intents = discord.Intents.default()
intents.message_content = True  # Privileged intent
intents.presences = True  # Track online status
intents.members = True  # Required for the on_member_join event
intents.voice_states = True  # Track voice state changes

bot = commands.Bot(command_prefix=config.PREFIX, intents=intents, help_command=None)


def configure_logging() -> None:
    """Set up human-readable logging for the bot and its dependencies."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # The Appwrite SDK logs through urllib3 — keep it quiet unless it matters.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# Event: When the bot is ready
@bot.event
async def on_ready():
    LOG.info("Logged in as %s (latency %.1f ms)", bot.user, bot.latency * 1000)
    await sync_commands()


async def sync_commands():
    """Register hybrid slash commands with Discord.

    With GUILD_ID set, commands sync instantly to that server (great for
    testing); otherwise they sync globally and can take up to an hour.
    """
    if config.GUILD_ID:
        guild = discord.Object(id=config.GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
    else:
        synced = await bot.tree.sync()
    LOG.info("Synced %d slash command(s)", len(synced))


async def _respond(ctx: commands.Context, content: str, ephemeral: bool = True):
    """Reply gracefully to both prefix and slash invocations."""
    if ctx.interaction:
        await ctx.interaction.response.send_message(content, ephemeral=ephemeral)
    else:
        await ctx.send(content)


# Event: Prefix-based errors
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(f"⛔ You need `{', '.join(error.missing_permissions)}` to use that.")
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Slow down! Try again in {error.retry_after:.0f}s.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: `{error.param.name}`.")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Bad argument: {error}")
        return
    LOG.error("Command %r failed: %s", ctx.command, error)
    await ctx.send("⚠️ Something went wrong.")


# Event: Slash/hybrid errors
@bot.event
async def on_application_command_error(
    interaction: discord.Interaction, error: discord.app_commands.AppCommandError
):
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        content = f"⏳ Slow down! Try again in {error.retry_after:.0f}s."
    elif isinstance(error, discord.app_commands.MissingPermissions):
        content = f"⛔ You need `{', '.join(error.missing_permissions)}` to use that."
    else:
        LOG.error("Slash command %r failed: %s", interaction.command, error)
        content = "⚠️ Something went wrong."
    try:
        await interaction.response.send_message(content, ephemeral=True)
    except discord.InteractionResponded:
        await interaction.followup.send(content, ephemeral=True)


async def load_cogs():
    """Auto-discover and load every cog in the cogs/ package.

    Drop a new file in cogs/ and it is picked up automatically; no wiring
    needed in this launcher.
    """
    cogs_dir = Path("cogs")
    for path in sorted(cogs_dir.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        extension = f"cogs.{path.stem}"
        await bot.load_extension(extension)
        LOG.info("Loaded cog: %s", extension)


async def main():
    configure_logging()
    try:
        await store.init()
    except Exception as exc:  # keep the bot alive even if Appwrite is down
        LOG.critical("Appwrite store unavailable: %s — continuing without persistence", exc)
    await load_cogs()
    await bot.start(config.BOT_TOKEN)

# Run the bot
if __name__ == "__main__":
    asyncio.run(main())