#!/usr/bin/env python3
"""Verify Bot_CR can reach its TablesDB backend (robotics_hub).

The bot is a *consumer* of the hub: databases, tables and columns are
provisioned through the console / MCP by the web side and must never be
created here. This script only:

* checks connectivity and that every table the bot expects is present,
* idempotently seeds the club_roles *content* rows the permission hierarchy
  depends on (rows are content, not schema).

Mirrors what ``await store.init()`` does, so a green bootstrap is a green bot
start. Uses values from .env (APPWRITE_ENDPOINT / APPWRITE_PROJECT_ID /
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

import config  # noqa: E402
from data.appwrite_client import (  # noqa: E402
    HUB_TABLES,
    build_tables_client,
    connectivity_check,
    seed_club_roles,
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    tdb = build_tables_client()
    db_id = config.APPWRITE_DATABASE_ID
    tables = connectivity_check(tdb, db_id)
    missing = [name for name in HUB_TABLES if name not in tables]
    if missing:
        print(f"\n✗ Hub tables missing from {db_id}: {', '.join(missing)}")
        sys.exit(1)
    created = seed_club_roles(tdb, db_id)
    print(f"\n✓ {db_id} ready: {len(tables)} tables found, "
          f"{len(created)} club_roles row(s) seeded.")


if __name__ == "__main__":
    main()