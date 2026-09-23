"""TablesDB connectivity + content seeding for Bot_CR (robotics_hub).

The bot is a *consumer* of the club's Appwrite TablesDB backend (robotics_hub,
Appwrite server 1.9.6) and must never create databases, tables or columns at
runtime — the hub schema is provisioned through the console / MCP and owned by
the web side. This module only:

* builds the TablesDB SDK client (endpoint / project / key / database),
* verifies connectivity by listing tables,
* seeds the ``club_roles`` *content* rows the bot's permission hierarchy
  depends on (rows are content, not schema).

Row conventions used by the whole bot (see data/store.py):
* row ids are bot-chosen, ≤ 36 chars, contain no ``/``,
* datetimes are ISO-8601 strings with an offset (``+00:00``), never naive,
* relationship columns are plain FK strings holding the related row ``$id``
  (null when unset); parent-side relationship columns are written only.
"""

from __future__ import annotations

import logging

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.services.tables_db import TablesDB

import config

LOG = logging.getLogger("bot.appwrite")

# Every table the bot expects to find in the hub. Used by the bootstrap /
# connectivity check only — the bot never creates any of them.
HUB_TABLES: tuple[str, ...] = (
    "seasons",
    "departments",
    "club_roles",
    "members",
    "member_socials",
    "memberships",
    "discord_users",
    "minecraft_accounts",
    "discord_mc_links",
    "member_discord_links",
    "minecraft_otp",
    "warnings",
    "tasks",
    "reports",
    "events",
    "competitions",
    "polls",
    "member_stats",
    "bot_settings",
    "modlog",
    "discord_data",
)

# club_roles seed rows. ``$id`` == the legacy hierarchy key used across
# cogs/_scopes.py so memberships can store the FK directly; ``weight`` is the
# rank (higher wins); ``is_lead`` gates the elevated scalar scopes.
CLUB_ROLE_SEED: tuple[tuple[str, str, int, bool], ...] = (
    ("core_member", "Core Member", 0, False),
    ("cell_member", "Cell Member", 1, False),
    ("cell_chief", "Cell Chief", 2, True),
    ("vice_president", "Vice President", 3, True),
    ("president", "President", 4, True),
    ("archon", "Archon", 5, True),
)


def build_tables_client() -> TablesDB:
    """A configured TablesDB service client for the configured project."""
    native = (
        Client()
        .set_endpoint(config.APPWRITE_ENDPOINT)
        .set_project(config.APPWRITE_PROJECT_ID)
        .set_key(config.APPWRITE_API_KEY)
    )
    return TablesDB(native)


def is_missing(exc: AppwriteException) -> bool:
    """True when the failure is a plain resource-not-found (404)."""
    return exc.code == 404 or exc.type in (
        "document_not_found",
        "row_not_found",
        "database_not_found",
        "table_not_found",
    )


def connectivity_check(tdb: TablesDB, database_id: str) -> list[str]:
    """List the hub tables; raises AppwriteException when keys/db are wrong."""
    result = tdb.list_tables(database_id=database_id, total=True)
    return [table.id for table in result.tables]


def seed_club_roles(tdb: TablesDB, database_id: str) -> list[str]:
    """Idempotently create the club_roles rows the bot's hierarchy reads.

    Content only — the table itself belongs to the hub. Missing rows are
    created straight from CLUB_ROLE_SEED; existing rows are left untouched.
    Returns the list of role keys that were actually created.
    """
    created: list[str] = []
    for key, title, weight, is_lead in CLUB_ROLE_SEED:
        try:
            tdb.get_row(
                database_id=database_id, table_id="club_roles", row_id=key
            )
        except AppwriteException as exc:
            if not is_missing(exc):
                raise
            tdb.create_row(
                database_id=database_id,
                table_id="club_roles",
                row_id=key,
                data={
                    "title": title,
                    "weight": weight,
                    "is_lead": is_lead,
                },
            )
            created.append(key)
            LOG.info("Seeded club role %r (title=%r)", key, title)
    return created