#!/usr/bin/env python3
"""Hermetic smoke test: compile-free verification that every cog loads.

This does NOT touch the network or Appwrite — it only builds a bot, loads every
extension in cogs/, and checks the command surface. Runtime env vars are read
from the environment (CI injects dummy values; locally the real .env is used).

Usage:
    BOT_TOKEN=x APPWRITE_API_KEY=y python scripts/smoke_test.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import asyncio  # noqa: E402

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

EXPECTED_COMMANDS = {
    "8ball", "addrole", "advice", "assign", "ban", "bot", "cancel", "cell",
    "challenge",
    "choose", "claim", "clap", "close", "coinflip", "competition",
    "competitions", "complete", "compliment", "create", "crypto",
    "current_voice_time",
    "dashboard", "define", "del", "dice", "edit", "end", "event", "events",
    "fact", "fixname", "github", "hierarchy", "hello", "help", "history",
    "hug", "joke", "kick", "language", "leaderboard", "link", "linkmc", "list", "lock", "loop", "lyrics",
    "mc", "mclink", "mcpass", "mcrestart", "mcsession", "mcstart", "mcstop", "meeting", "meme", "minecraft", "modlog", "mute", "notifications", "nowplaying",
    "online_members", "overdue", "pause", "ping", "play", "poll", "profile", "queue",
    "quiz", "quote", "rank", "removerole", "resume", "results", "reverse", "roast",
    "robot", "roles", "rps", "set", "setlead", "setprofile", "settings", "ship", "skip",
    "slap", "slowmode", "spacex", "stop", "task", "tasks", "timeout", "today",
    "total_messages", "total_voice_time", "unban", "unlink", "unlock", "unmute",
    "untimeout", "view", "volume", "vote", "warn", "weather", "website", "whois",
}


def main() -> int:
    # config requires these; fail loudly (not cryptically) if they're missing.
    for var in ("BOT_TOKEN", "APPWRITE_API_KEY"):
        if not os.getenv(var):
            print(f"✗ Missing required env var {var} (set a dummy value for this test)")
            return 1

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.all(), help_command=None)

    async def run() -> None:
        cogs_dir = Path("cogs")
        loaded = 0
        for path in sorted(cogs_dir.glob("*.py")):
            if path.name.startswith("_") or path.name == "__init__.py":
                continue
            extension = f"cogs.{path.stem}"
            await bot.load_extension(extension)
            loaded += 1

        names = {c.name for c in bot.walk_commands()}
        missing = EXPECTED_COMMANDS - names
        non_hybrid = [
            c.name for c in bot.walk_commands()
            if not isinstance(c, (commands.HybridCommand, commands.HybridGroup))
        ]
        if missing:
            print(f"✗ Missing expected commands: {sorted(missing)}")
            raise SystemExit(1)
        if non_hybrid:
            print(f"✗ Non-hybrid commands: {non_hybrid}")
            raise SystemExit(1)
        if bot.help_command is not None:
            print("✗ Custom help was not installed (help_command must be None)")
            raise SystemExit(1)
        if "help" not in names:
            print("✗ No custom help command registered")
            raise SystemExit(1)

        print(f"✓ Loaded {loaded} cogs and {len(names)} commands (all hybrid, incl. custom help)")

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - test runner
        print(f"✗ Smoke test failed: {exc}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())