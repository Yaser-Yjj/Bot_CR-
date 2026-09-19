"""Unit checks for the /bot status update checker + console join/leave parser.

Run:  .venv-local/bin/python scripts/test_updates.py
Needs dummy BOT_TOKEN/APPWRITE_API_KEY (like smoke_test) so config imports.
The git-state test fetches origin/nightly from this checkout (local, cached).
"""

import asyncio
import os
import sys
import unittest
from unittest import IsolatedAsyncioTestCase

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs.bot_admin import _git_update_log, _git_update_state  # noqa: E402
from cogs.minecraft import Minecraft  # noqa: E402


class ConsoleParsingTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cog = Minecraft(bot=None)
        self.cog._online = {}
        self.cog._pending_rally = set()
        self.cog._rally_task = None

    def tearDown(self) -> None:
        if self.cog._rally_task is not None:
            self.cog._rally_task.cancel()

    async def test_join_lines(self):
        for line in (
            "[Server thread/INFO]: Steve joined the game",
            "[Server thread/INFO]: Alex_123 joined the game",
            "[Server thread/WARN]: Notch joined the game",
        ):
            self.cog._consume_console_line(line)
        self.assertEqual(sorted(self.cog._online), ["Alex_123", "Notch", "Steve"])
        # The same name joining again must not double-track nor re-rally.
        self.cog._consume_console_line("[Server thread/INFO]: Steve joined the game")
        self.assertEqual(len(self.cog._online), 3)
        self.assertEqual(self.cog._pending_rally,
                         {"Steve", "Alex_123", "Notch"})

    async def test_leave_lines(self):
        self.cog._consume_console_line("[Server thread/INFO]: Steve joined the game")
        self.cog._consume_console_line("[Server thread/INFO]: Alex joined the game")
        for line in (
            "[Server thread/INFO]: Steve left the game",
            "[Server thread/INFO]: Alex lost connection: TextComponent{text='Timed out'}",
        ):
            self.cog._consume_console_line(line)
        self.assertEqual(self.cog._online, {})

    def test_junk_lines_ignored(self):
        for line in (
            "[Server thread/INFO]: Done (3.2s)! For help, type \"help\"",
            "[Async Chat Thread - #1/INFO]: <Steve> hello everyone",
            "[Server thread/INFO]: Steve issued server command: /op Steve",
        ):
            self.cog._consume_console_line(line)
        self.assertEqual(self.cog._online, {})

    async def test_rally_scheduling_burst_coalesces(self):
        for name in ("a", "b", "c"):
            self.cog._consume_console_line(
                f"[Server thread/INFO]: {name} joined the game")
        # One shared flush task for the whole burst; the names pile up in the
        # pending window and are NOT posted straight away (10 s coalesce).
        self.assertIsNotNone(self.cog._rally_task)
        self.assertFalse(self.cog._rally_task.done())
        self.assertEqual(self.cog._pending_rally, {"a", "b", "c"})
        # A second burst reuses the same task instead of stacking another one.
        first = self.cog._rally_task
        self.cog._consume_console_line("[Server thread/INFO]: d joined the game")
        self.assertIs(self.cog._rally_task, first)
        self.assertEqual(self.cog._pending_rally, {"a", "b", "c", "d"})
        # Cooldown already active -> queued names are dropped, nothing posted.
        self.cog._rally_cooldown_until = float("inf")
        await self.cog._flush_rally()
        self.assertEqual(self.cog._pending_rally, set())


class GitStateTests(unittest.TestCase):
    def test_update_state_shape(self):
        behind, ahead, remote, err = _git_update_state(force=True)
        self.assertIsNone(err, f"expected no error, got {err!r}")
        self.assertIsNotNone(remote)
        self.assertIsInstance(behind, int)
        self.assertIsInstance(ahead, int)
        self.assertGreaterEqual(behind, 0)
        self.assertGreaterEqual(ahead, 0)

    def test_update_log_format(self):
        lines = _git_update_log(3)
        self.assertIsInstance(lines, list)
        for line in lines:
            self.assertRegex(line, r"^[0-9a-f]{7,} .+")

    def test_cached_result_shared(self):
        # Second call within the cache window must skip the fetch (returns data).
        behind, ahead, remote, err = _git_update_state()
        self.assertIsNone(err)


if __name__ == "__main__":
    unittest.main(verbosity=2)