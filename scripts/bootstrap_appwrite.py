#!/usr/bin/env python3
"""Provision Bot_CR's Appwrite schema (collections, attributes, indexes).

Idempotent: safe to re-run, missing pieces are created and nothing is
overwritten. Uses values from .env (APPWRITE_ENDPOINT / APPWRITE_PROJECT_ID /
APPWRITE_API_KEY / APPWRITE_DATABASE_ID).

Usage:
    python scripts/bootstrap_appwrite.py
"""

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.appwrite_client import ensure_schema  # noqa: E402


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    ensure_schema()
    print("\n✓ Appwrite schema is ready (bot_members, bot_counters, bot_challenges, "
          "bot_modlog, bot_settings, bot_polls, mc_link_codes, mc_auth, mc_challenges).")


if __name__ == "__main__":
    main()