"""Appwrite connectivity and idempotent schema bootstrap for Bot_CR.

All bot data (members, counters, challenges, modlog, settings) lives in the
club's Appwrite project. This module knows *how* to talk to Appwrite and what
the schema should look like; data/store.py wraps it in async, typed helpers.
"""

import logging

from appwrite.client import Client
from appwrite.enums.databases_index_type import DatabasesIndexType
from appwrite.exception import AppwriteException
from appwrite.services.databases import Databases

import config

LOG = logging.getLogger("bot.appwrite")

# ── Schema definition ──────────────────────────────────────────────────
# Collection id -> (display name, [attributes], [indexes])
COLLECTIONS = {
    "bot_members": (
        "Bot members",
        [
            {"key": "user_id", "type": "string", "size": 32, "required": True},
            {"key": "username", "type": "string", "size": 64},
            {"key": "display_name", "type": "string", "size": 64},
            {"key": "real_name", "type": "string", "size": 128},
            {"key": "birthday", "type": "string", "size": 16},
            {"key": "birthday_full", "type": "string", "size": 16},
            {"key": "joined_at", "type": "datetime"},
            {"key": "verified", "type": "boolean"},
            {"key": "xp", "type": "integer"},
            {"key": "messages", "type": "integer"},
            {"key": "voice_seconds", "type": "float"},
            {"key": "warnings", "type": "integer"},
            {"key": "last_xp_at", "type": "string", "size": 32},
            # Linked club account (Discord -> club account -> role -> cell).
            {"key": "club_id", "type": "string", "size": 64},
            {"key": "club_role", "type": "string", "size": 32},
            {"key": "cell", "type": "string", "size": 64},
            # Notification preferences (1 = enabled).
            {"key": "notify_tasks", "type": "boolean"},
            {"key": "notify_events", "type": "boolean"},
            {"key": "notify_competitions", "type": "boolean"},
            {"key": "notify_announcements", "type": "boolean"},
            # Minecraft: canonical name for back-compat + per-platform identity
            # map (JSON string; Appwrite has no object type). Written by
            # _save_mc_link, read through Minecraft._links_of.
            {"key": "mc_username", "type": "string", "size": 64},
            {"key": "links", "type": "string", "size": 4096},
        ],
        [
            {"key": "uniq_user", "type": "unique", "attributes": ["user_id"]},
            {"key": "by_messages", "type": "key", "attributes": ["messages"]},
            {"key": "by_xp", "type": "key", "attributes": ["xp"]},
        ],
    ),
    "bot_counters": (
        "Bot global counters",
        [
            {"key": "total_messages", "type": "integer", "required": True},
            {"key": "total_voice_seconds", "type": "float", "required": True},
            {"key": "boot_at", "type": "datetime"},
            {"key": "last_flush_at", "type": "datetime"},
        ],
        [],
    ),
    "bot_challenges": (
        "Daily challenges",
        [
            {"key": "date", "type": "string", "size": 16, "required": True},
            {"key": "title", "type": "string", "size": 256, "required": True},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "created_by", "type": "string", "size": 32},
            {"key": "claimed", "type": "string", "array": True},
        ],
        [{"key": "by_date", "type": "key", "attributes": ["date"]}],
    ),
    "bot_modlog": (
        "Moderation log",
        [
            {"key": "action", "type": "string", "size": 32, "required": True},
            {"key": "target_id", "type": "string", "size": 32, "required": True},
            {"key": "target_name", "type": "string", "size": 64},
            {"key": "moderator_id", "type": "string", "size": 32},
            {"key": "moderator_name", "type": "string", "size": 64},
            {"key": "reason", "type": "string", "size": 1024},
            {"key": "created_at", "type": "datetime", "required": True},
        ],
        [{"key": "by_created_at", "type": "key", "attributes": ["created_at"]}],
    ),
    "bot_settings": (
        "Bot settings",
        [{"key": "value", "type": "string", "size": 4096, "required": True}],
        [],
    ),
    "bot_tasks": (
        "Club tasks",
        [
            {"key": "task_id", "type": "string", "size": 16, "required": True},
            {"key": "title", "type": "string", "size": 256, "required": True},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "assignee", "type": "string", "size": 32},
            {"key": "cell", "type": "string", "size": 64},
            {"key": "status", "type": "string", "size": 16},
            {"key": "priority", "type": "string", "size": 8},
            {"key": "due", "type": "string", "size": 16},
            {"key": "created_by", "type": "string", "size": 32},
            {"key": "created_at", "type": "datetime"},
        ],
        [{"key": "by_due", "type": "key", "attributes": ["due"]}],
    ),
    "bot_competitions": (
        "Club competitions",
        [
            {"key": "name", "type": "string", "size": 128, "required": True},
            {"key": "date", "type": "string", "size": 16},
            {"key": "location", "type": "string", "size": 128},
            {"key": "capacity", "type": "integer"},
            {"key": "registered", "type": "string", "array": True},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "created_at", "type": "datetime"},
        ],
        [],
    ),
    "bot_events": (
        "Club events",
        [
            {"key": "title", "type": "string", "size": 128, "required": True},
            {"key": "date", "type": "string", "size": 16},
            {"key": "time", "type": "string", "size": 16},
            {"key": "location", "type": "string", "size": 128},
            {"key": "description", "type": "string", "size": 2048},
            {"key": "attendees", "type": "string", "array": True},
            {"key": "declined", "type": "string", "array": True},
            {"key": "created_by", "type": "string", "size": 32},
            {"key": "created_at", "type": "datetime"},
        ],
        [],
    ),
    "bot_polls": (
        "Club polls",
        [
            {"key": "poll_id", "type": "string", "size": 16, "required": True},
            {"key": "question", "type": "string", "size": 512, "required": True},
            # Options are pipe-separated at creation; stored as one string each.
            {"key": "options", "type": "string", "size": 256, "array": True},
            # transparent (names visible) | anonymous (hashed voters only).
            {"key": "mode", "type": "string", "size": 16},
            # single (one choice, replaceable) | multiple (toggle per option).
            {"key": "selection", "type": "string", "size": 16},
            # Hide counts until the poll is closed (secret ballots).
            {"key": "hide_results", "type": "boolean"},
            {"key": "closed", "type": "boolean"},
            {"key": "closed_at", "type": "string", "size": 64},
            {"key": "created_by", "type": "string", "size": 32},
            {"key": "created_at", "type": "datetime"},
            # One JSON string per vote: {"v": id, "i": option idx, "t": iso}.
            {"key": "votes", "type": "string", "size": 256, "array": True},
        ],
        [{"key": "by_created_at", "type": "key", "attributes": ["created_at"]}],
    ),
    # ── mc-link (Discord ↔ Minecraft single sign-on) ───────────────────
    # Appwrite is the shared bus between this bot and the Paper plugin, and the
    # source of truth for identities and credentials. The bot is the ONLY
    # component that mints link codes and credentials; the plugin only claims
    # and consumes them. Minecraft itself is an untrusted client boundary — no
    # username/UUID/permission a client claims is ever treated as proof.
    "mc_link_codes": (
        "MC link one-time codes",
        [
            # 6 chars from ACDEFGHJKLMNPQRTUVWXY234679 (case-insensitive).
            {"key": "code", "type": "string", "size": 8, "required": True},
            {"key": "username", "type": "string", "size": 64, "required": True},
            {"key": "discord_id", "type": "string", "size": 32, "required": True},
            # pending | used | expired.
            {"key": "status", "type": "string", "size": 8, "required": True},
            {"key": "expires_at", "type": "datetime", "required": True},
            {"key": "created_at", "type": "datetime", "required": True},
            {"key": "used_at", "type": "datetime"},
            {"key": "claimed_ip", "type": "string", "size": 45},
        ],
        [
            {"key": "by_user_status", "type": "key",
             "attributes": ["username", "status"]},
            {"key": "by_status", "type": "key", "attributes": ["status"]},
        ],
    ),
    "mc_auth": (
        "MC per-username auth profiles",
        [
            # Exact/case-sensitive name — the ONLY identity key (no UUIDs, no
            # paid/free, no whitelist usage).
            {"key": "username", "type": "string", "size": 64, "required": True},
            {"key": "discord_id", "type": "string", "size": 32, "required": True},
            # bcrypt ($2a$…) of the current AuthMe password — server-side
            # concern; this bot never writes plaintext long-term passwords.
            {"key": "auth_hash", "type": "string", "size": 128},
            # JSON array [{ip, seen_at}].
            {"key": "last_ips", "type": "string", "size": 1024},
            # Live session IP (server-written; cleared on logout).
            {"key": "current_ip", "type": "string", "size": 45},
            {"key": "status", "type": "string", "size": 8},
            {"key": "linked_at", "type": "datetime"},
            {"key": "updated_at", "type": "datetime"},
        ],
        [{"key": "uniq_username", "type": "unique", "attributes": ["username"]}],
    ),
    "mc_challenges": (
        "MC credential jobs",
        [
            {"key": "username", "type": "string", "size": 64, "required": True},
            # new_ip | change_password.
            {"key": "kind", "type": "string", "size": 16, "required": True},
            # pending | approved | done | failed.
            {"key": "status", "type": "string", "size": 8, "required": True},
            # AES-256-GCM ciphertext (iv+tag+ct, hex) of the temp/new password.
            {"key": "payload_enc", "type": "string", "size": 2048},
            {"key": "ip", "type": "string", "size": 45},
            {"key": "expires_at", "type": "datetime"},
            {"key": "created_at", "type": "datetime", "required": True},
            {"key": "done_at", "type": "datetime"},
        ],
        [
            {"key": "by_user_status", "type": "key",
             "attributes": ["username", "status"]},
        ],
    ),
}


def build_client() -> Client:
    """Build a project-scoped Appwrite client from config."""
    client = Client()
    client.set_endpoint(config.APPWRITE_ENDPOINT)
    client.set_project(config.APPWRITE_PROJECT_ID)
    client.set_key(config.APPWRITE_API_KEY)
    return client


def build_databases() -> Databases:
    """Build the Databases service (not bound to a specific database)."""
    return Databases(build_client())


def is_missing(exc: AppwriteException) -> bool:
    """True when an Appwrite exception is a 404 / not_found."""
    return getattr(exc, "code", None) == 404 or "not_found" in (getattr(exc, "type", "") or "")


def ensure_database(db: Databases) -> None:
    """Create the configured database if it does not exist yet."""
    db_id = config.APPWRITE_DATABASE_ID
    try:
        db.get(db_id)
    except AppwriteException as exc:
        if not is_missing(exc):
            raise
        db.create(db_id, f"Bot_CR data ({db_id})", enabled=True)
        LOG.info("Created database %r", db_id)


def _create_attribute(db: Databases, db_id: str, collection_id: str, spec: dict) -> None:
    """Create a single attribute on an existing collection."""
    key = spec["key"]
    kind = spec["type"]
    required = bool(spec.get("required", False))
    default = spec.get("default")
    array = spec.get("array")
    if kind == "string":
        size = spec.get("size", 64)
        db.create_string_attribute(db_id, collection_id, key, size, required,
                                   default=default, array=array)
    elif kind == "integer":
        db.create_integer_attribute(db_id, collection_id, key, required,
                                    default=default, array=array)
    elif kind == "float":
        db.create_float_attribute(db_id, collection_id, key, required,
                                  default=default, array=array)
    elif kind == "boolean":
        db.create_boolean_attribute(db_id, collection_id, key, required,
                                    default=default, array=array)
    elif kind == "datetime":
        db.create_datetime_attribute(db_id, collection_id, key, required,
                                     default=default, array=array)
    else:  # pragma: no cover - schema is internal
        raise ValueError(f"Unsupported attribute type: {kind}")
    LOG.info("Created attribute %s.%s.%s", db_id, collection_id, key)


def ensure_schema(db: Databases | None = None) -> None:
    """Idempotently ensure every collection Bot_CR needs exists.

    Safe to call on every boot: missing pieces are created, existing ones are
    left untouched. New attributes/indexes are back-filled where possible.
    """
    db = db or build_databases()
    db_id = config.APPWRITE_DATABASE_ID
    ensure_database(db)

    for collection_id, (name, attributes, indexes) in COLLECTIONS.items():
        try:
            existing = db.get_collection(db_id, collection_id)
        except AppwriteException as exc:
            if not is_missing(exc):
                raise
            existing = None

        if existing is None:
            db.create_collection(
                db_id,
                collection_id,
                name,
                permissions=[],
                document_security=False,
                enabled=True,
            )
            LOG.info("Created collection %r", collection_id)

        # Attributes must be created one by one through their dedicated
        # endpoints (the inline `attributes` payload is unreliable across
        # Appwrite versions). Idempotent: existing attributes are kept.
        if existing is None:
            existing = db.get_collection(db_id, collection_id)
        have_attrs = {attr.key for attr in (existing.attributes or [])}
        for spec in attributes:
            if spec["key"] not in have_attrs:
                _create_attribute(db, db_id, collection_id, spec)

        have_indexes = {idx.key for idx in (existing.indexes or [])}
        for spec in indexes:
            if spec["key"] not in have_indexes:
                db.create_index(
                    db_id,
                    collection_id,
                    spec["key"],
                    DatabasesIndexType(spec["type"]),
                    spec["attributes"],
                )
                LOG.info("Created index %s on %r", spec["key"], collection_id)