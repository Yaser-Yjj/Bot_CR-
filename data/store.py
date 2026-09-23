"""Async, typed access to Bot_CR's TablesDB backend (robotics_hub).

The bot consumes the club's web-owned Appwrite TablesDB (server 1.9.6) and
never creates databases/tables/columns at runtime — the schema is provisioned
through the console / MCP. The Appwrite Python SDK is synchronous and
blocking, so every call is dispatched through ``asyncio.to_thread`` to keep
the Discord gateway loop responsive. High-frequency events (messages, XP,
voice time) are accumulated in-memory by the stats/engagement cogs and flushed
periodically through ``flush_member_activity`` / ``bump_counters``.

Identity model
--------------
* ``discord_users.$id == str(discord_id)`` — Discord-side identity
  (username / display_name / avatar_url / joined_at).
* ``discord_data.$id == str(discord_id)`` — Discord-only stats, birthdays,
  language, notification flags and real name.
* ``members.$id == str(discord_id)`` — club-side identity, created *lazily*
  only when club state (role/cell/link/warn/task reference) needs it;
  ``auth_user_id`` holds the club account id from /link.
* ``memberships.$id == str(discord_id)`` — the member's current club role
  (``role`` FK -> club_roles.$id which is the legacy hierarchy key) and cell
  (``department`` FK -> departments, best-effort).
* ``warnings`` rows (card_type="yellow" — the hub enum, used for warns) —
  one row per warning; ``get_member`` counts them so the flat record keeps
  its legacy ``warnings`` integer.
* freetext Discord-only mirrors that have no hub column (the ``links`` map,
  ``mc_username``, the raw cell label when no department row matches) live in
  a ``bot_settings`` sidecar key ``member_side:{uid}``.

Row ids are bot-chosen (≤ 36 chars, no ``/``), datetimes are ISO-8601 with
offset, and FK columns are plain strings of the related row ``$id`` (null
when unset).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from appwrite.exception import AppwriteException
from appwrite.id import ID
from appwrite.query import Query

from data.appwrite_client import (
    build_tables_client,
    connectivity_check as _connectivity_sync,
    is_missing,
    seed_club_roles as _seed_roles_sync,
)

LOG = logging.getLogger("bot.store")

# Hub table id shorthand (kept explicit so the mapping reads at a glance).
_T = {
    "members": "members",
    "memberships": "memberships",
    "club_roles": "club_roles",
    "departments": "departments",
    "discord_users": "discord_users",
    "discord_data": "discord_data",
    "member_discord_links": "member_discord_links",
    "minecraft_accounts": "minecraft_accounts",
    "discord_mc_links": "discord_mc_links",
    "minecraft_otp": "minecraft_otp",
    "warnings": "warnings",
    "tasks": "tasks",
    "events": "events",
    "competitions": "competitions",
    "polls": "polls",
    "modlog": "modlog",
    "bot_settings": "bot_settings",
}

# Defaults used when a discord_data row has to be bootstrapped (REQ columns).
_DD_DEFAULTS = {"xp": 0, "messages": 0, "voice_seconds": 0, "verified": False}

# Far-future sentinel for REQUIRED datetime columns (event/comp dates): keeps
# the "no date = always upcoming" behaviour and round-trips back to "".
_EVENT_NO_DATE = "9999-12-31T23:59:59+00:00"

# Legacy hierarchy keys (== club_roles.$id) the bot writes as role FKs.
_KNOWN_ROLES = frozenset(
    ("core_member", "cell_member", "cell_chief",
     "vice_president", "president", "archon")
)

_EMPTY_NOTIFY = {
    "notify_tasks": True,
    "notify_events": True,
    "notify_competitions": True,
    "notify_announcements": True,
}


class StoreError(RuntimeError):
    """Raised when an Appwrite operation fails."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_text(value) -> str:
    """Any datetime/str -> displayable ISO string ('' for None)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _to_iso_datetime(text) -> str | None:
    """Normalize a user "YYYY-MM-DD" / ISO date into an offset datetime."""
    text = str(text or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _norm_iso(text: str) -> str:
    """Normalize a hub datetime string (may carry ``.000`` ms) to a clean\
    offset ISO string; non-datetime text passes through untouched."""
    text = str(text or "")
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


def _parse_ts(value) -> datetime | None:
    """Parse a hub timestamp (ISO-8601, may end in Z) into a tz-aware
    datetime; None on any unparseable/empty input (never raises)."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _event_date_in(text) -> str:
    """Turn a free-text event/competition date into a hub datetime (REQ)."""
    return _to_iso_datetime(text) or _EVENT_NO_DATE


def _event_date_out(value) -> str:
    """Round a hub datetime back: sentinel -> '' (no date), else clean ISO."""
    text = _iso_text(value)
    if text.startswith("9999-"):
        return ""  # sentinel round-trips back to "no date"
    return _norm_iso(text)


def _as_json(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[]"


def _from_json(value, default=None):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (ValueError, TypeError):
        return default


def _rel_id(value) -> str:
    """Relationship columns can come back as the raw id string."""
    if isinstance(value, dict):
        return str(value.get("$id") or value.get("id") or "")
    return str(value or "")


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class Store:
    """Typed facade over the TablesDB robotics_hub backend."""

    def __init__(self):
        self._tdb = None
        self._db_id = None
        self._dept_cache: list[dict] | None = None

    # ── lifecycle ──────────────────────────────────────────────
    async def init(self) -> None:
        """Connect to the hub, verify tables, seed club_roles content only."""
        self._tdb = build_tables_client()
        from config import APPWRITE_DATABASE_ID  # avoid import cycle
        self._db_id = APPWRITE_DATABASE_ID
        await asyncio.to_thread(_connectivity_sync, self._tdb, self._db_id)
        await asyncio.to_thread(_seed_roles_sync, self._tdb, self._db_id)
        LOG.info("TablesDB store ready (db=%s)", self._db_id)

    async def ensure_schema(self) -> None:
        """Connectivity check only — the bot never owns schema."""
        tdb = self._tdb or build_tables_client()
        from config import APPWRITE_DATABASE_ID  # avoid import cycle
        db_id = self._db_id or APPWRITE_DATABASE_ID
        await asyncio.to_thread(_connectivity_sync, tdb, db_id)

    def _raw(self):
        if self._tdb is None or self._db_id is None:
            raise StoreError("Store not initialised — call await store.init() first")
        return self._tdb, self._db_id

    # ── low-level helpers ─────────────────────────────────────
    @staticmethod
    def _row_data(row) -> dict:
        """Plain data dict + $id/$createdAt/$updatedAt meta (legacy shape)."""
        data = dict(row.data or {})
        data["$id"] = row.id
        data["$createdAt"] = _iso_text(row.createdat)
        data["$updatedAt"] = _iso_text(row.updatedat)
        return data

    async def _get(self, table: str, row_id: str) -> dict | None:
        tdb, db_id = self._raw()
        try:
            row = await asyncio.to_thread(
                tdb.get_row, db_id, table, row_id)
        except AppwriteException as exc:
            if is_missing(exc):
                return None
            raise StoreError(f"get {table}/{row_id}: {exc}") from exc
        return self._row_data(row)

    async def _create(self, table: str, row_id: str, data: dict) -> str:
        """Create a row; returns its ``$id`` (pass ``ID.unique()`` for random)."""
        tdb, db_id = self._raw()
        try:
            row = await asyncio.to_thread(
                tdb.create_row, db_id, table, row_id, data)
        except AppwriteException as exc:
            raise StoreError(f"create {table}/{row_id}: {exc}") from exc
        return row.id

    async def _patch(self, table: str, row_id: str, data: dict) -> None:
        """Partial update — only the given columns change; None clears.

        TablesDB quirk (verified live): a PATCH on a table with relationship
        columns must re-declare EVERY relationship column (as an id string or
        null) or the server 400s with ``relationship_value_invalid``. Columns
        the caller doesn't mention are filled from the row's current values,
        so a caller changing {closed: true} on a poll never has to know about
        its ``created_by`` relationship.
        """
        data = {k: v for k, v in data.items()}
        if not data:
            return  # nothing to update; the SDK rejects empty patches
        rel_cols = await self._rel_columns(table)
        if rel_cols:
            missing = [c for c in rel_cols if c not in data]
            if missing:
                current = await self._get(table, row_id)
                if current is not None:
                    for col in missing:
                        data[col] = current.get(col) or None
        tdb, db_id = self._raw()
        try:
            await asyncio.to_thread(
                tdb.update_row, db_id, table, row_id, data)
        except AppwriteException as exc:
            raise StoreError(f"patch {table}/{row_id}: {exc}") from exc

    # Relationship columns per table, discovered once from the live schema
    # (list_tables returns full column metadata). Used by _patch to satisfy
    # the server's re-declaration rule above.
    _rel_cache: dict[str, tuple[str, ...]] | None = None

    async def _rel_columns(self, table: str) -> tuple[str, ...]:
        if self._rel_cache is None:
            cache: dict[str, tuple[str, ...]] = {}
            try:
                tdb, db_id = self._raw()
                result = await asyncio.to_thread(tdb.list_tables, db_id)
                for entry in result.tables:
                    d = entry.to_dict() if hasattr(entry, "to_dict") else entry
                    rels = tuple(c["key"] for c in d.get("columns", [])
                                 if c.get("type") == "relationship")
                    if rels:
                        cache[d.get("$id")] = rels
            except AppwriteException as exc:
                LOG.warning("could not inspect table relationships: %s", exc)
                cache = {}
            self._rel_cache = cache
        return self._rel_cache.get(table, ())

    async def _write(self, table: str, row_id: str, data: dict,
                     *, defaults: dict | None = None) -> None:
        """Patch an existing row, or create it with ``defaults`` + data.

        Partial-update semantics everywhere instead of full-row upserts: hub
        columns are stricker than the legacy free-form docs, and every call
        site knows exactly which values it wants to touch.
        """
        existing = await self._get(table, row_id)
        if existing is None:
            payload = dict(defaults or {})
            payload.update({k: v for k, v in data.items()})
            await self._create(table, row_id, payload)
        else:
            await self._patch(table, row_id, data)

    async def _list(self, table: str, queries: list[str]) -> list[dict]:
        tdb, db_id = self._raw()
        try:
            result = await asyncio.to_thread(
                tdb.list_rows, db_id, table, queries=queries)
            return [self._row_data(r) for r in result.rows]
        except AppwriteException as exc:
            if is_missing(exc):
                return []
            raise StoreError(f"list {table}: {exc}") from exc

    async def _listed(self, table: str, limit: int, *, order_by: str | None = None,
                      queries: list[str] | None = None) -> list[dict]:
        """Page an unbounded filter (25 rows/page) into up to ``limit`` rows.

        ``order_by`` sorts desc on that column; results are returned in that
        order. Used instead of Query.order_* + limit directly because hub list
        endpoints cap pages at 25 rows.
        """
        out: list[dict] = []
        offset = 0
        while len(out) < limit:
            qs = list(queries or [])
            if order_by:
                qs.append(Query.order_desc(order_by))
            qs.append(Query.limit(25))
            qs.append(Query.offset(offset))
            page = await self._list(table, qs)
            out.extend(page)
            if len(page) < 25:
                break
            offset += 25
        return out[:limit]

    async def _get_rows_batch(self, table: str, ids) -> dict[str, dict]:
        """Fetch many rows by $id with a handful of equal-array queries."""
        ids = [str(i) for i in ids if str(i)]
        out: dict[str, dict] = {}
        for start in range(0, len(ids), 25):
            chunk = ids[start:start + 25]
            rows = await self._list(
                table, [Query.equal("$id", chunk), Query.limit(25)])
            for row in rows:
                out[row["$id"]] = row
        return out

    async def _count(self, table: str, queries: list[str] | None = None,
                     *, cap: int = 250) -> int:
        total = 0
        offset = 0
        while total < cap:
            qs = [*(queries or []), Query.limit(25), Query.offset(offset)]
            page = await self._list(table, qs)
            total += len(page)
            if len(page) < 25:
                break
            offset += 25
        return total

    # ── settings (bot_settings sidecar + generic) ─────────────
    async def get_setting(self, key: str) -> str | None:
        row = await self._get(_T["bot_settings"], key)
        if row is None:
            return None
        value = row.get("value")
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    async def set_setting(self, key: str, value: str) -> None:
        await self._write(_T["bot_settings"], key, {"value": value},
                          defaults={"key": key, "value": value})

    async def _batch_settings(self, keys) -> dict[str, str]:
        keys = [k for k in keys if k]
        out: dict[str, str] = {}
        for start in range(0, len(keys), 25):
            chunk = keys[start:start + 25]
            rows = await self._list(
                _T["bot_settings"], [Query.equal("key", chunk), Query.limit(25)])
            for row in rows:
                key = row.get("key") or ""
                if key in chunk:
                    out[key] = _iso_text(row.get("value")) or ""
        return out

    async def _read_sidecar(self, uid: str) -> dict:
        raw = await self.get_setting(f"member_side.{uid}")
        if not raw:
            return {}
        value = _from_json(raw, {})
        return value if isinstance(value, dict) else {}

    async def _write_sidecar(self, uid: str, **updates) -> None:
        side = await self._read_sidecar(uid)
        side.update({k: v for k, v in updates.items() if v is not None})
        await self.set_setting(f"member_side.{uid}", json.dumps(side, ensure_ascii=False))

    async def _sidecar_batch(self, uids) -> dict[str, dict]:
        rows = await self._batch_settings([f"member_side.{u}" for u in uids])
        out: dict[str, dict] = {}
        for key, raw in rows.items():
            value = _from_json(raw, {})
            if isinstance(value, dict):
                out[key[len("member_side."):]] = value
        return out

    # ── departments (cell FK resolution) ──────────────────────
    async def _departments(self) -> list[dict]:
        if self._dept_cache is None:
            self._dept_cache = await self._listed(_T["departments"], 100)
        return self._dept_cache

    async def _resolve_department(self, cell: str) -> str | None:
        """Best-effort cell -> departments FK (matches by label or id)."""
        key = str(cell or "").strip().lower()
        if not key:
            return None
        for row in await self._departments():
            if str(row["$id"]).lower() == key or \
               str(row.get("label") or "").lower() == key:
                return row["$id"]
        return None

    async def _cell_display(self, mship: dict | None, side_cell: str) -> str:
        dept_id = _rel_id((mship or {}).get("department"))
        if dept_id:
            for row in await self._departments():
                if row["$id"] == dept_id:
                    return str(row.get("label") or dept_id)
            return dept_id
        return side_cell or ""

    # ── members (flat legacy record shape) ────────────────────
    async def get_member(self, user_id: int) -> dict | None:
        uid = str(user_id)
        duser = await self._get(_T["discord_users"], uid)
        ddata = await self._get(_T["discord_data"], uid)
        if duser is None and ddata is None:
            return None  # never seen — same contract as the legacy collection
        mship = await self._get(_T["memberships"], uid)
        mem = await self._get(_T["members"], uid)
        side = await self._read_sidecar(uid)
        warnings = await self._count(
            _T["warnings"], [Query.equal("member", uid)])
        return await self._assemble(uid, duser, ddata, mship, mem, side, warnings)

    async def _assemble(self, uid: str, duser: dict | None, ddata: dict | None,
                        mship: dict | None, mem: dict | None,
                        side: dict | None, warnings: int = 0) -> dict:
        side = side or {}
        rec = {
            "$id": uid,
            "user_id": uid,
            "username": (duser or {}).get("username") or "",
            "display_name": (duser or {}).get("display_name") or "",
            "avatar_url": (duser or {}).get("avatar_url") or "",
            "joined_at": (duser or {}).get("joined_at") or None,
            "real_name": (ddata or {}).get("real_name") or "",
            "birthday": (ddata or {}).get("birthday") or "",
            "birthday_full": (ddata or {}).get("birthday_full") or "",
            "verified": bool((ddata or {}).get("verified")),
            "xp": int((ddata or {}).get("xp") or 0),
            "messages": int((ddata or {}).get("messages") or 0),
            "voice_seconds": int((ddata or {}).get("voice_seconds") or 0),
            "last_xp_at": _iso_text((ddata or {}).get("last_xp_at")),
            "lang": (ddata or {}).get("lang") or "",
            "warnings": warnings,
            "club_id": (mem or {}).get("auth_user_id") or "",
            "club_role": _rel_id((mship or {}).get("role")),
            "cell": await self._cell_display(mship, side.get("cell") or ""),
            "links": side.get("links"),
            "mc_username": side.get("mc_username") or "",
        }
        for key in _EMPTY_NOTIFY:
            value = (ddata or {}).get(key)
            rec[key] = bool(_EMPTY_NOTIFY[key] if value is None else value)
        return rec

    async def merge_member(self, user_id: int, data: dict) -> None:
        """Create-or-update a member, scattering the flat payload across the
        hub tables (see module docstring for the identity model)."""
        uid = str(user_id)

        # Discord-side identity -> discord_users.
        du = {k: data[k] for k in
              ("username", "display_name", "avatar_url", "joined_at") if k in data}
        if du:
            await self._write(_T["discord_users"], uid, du,
                              defaults={"username": du.get("username") or ""})

        # Discord-only stats/prefs -> discord_data.
        dd = {k: data[k] for k in (
            "real_name", "verified", "birthday", "birthday_full", "lang",
            "last_xp_at", "notify_tasks", "notify_events",
            "notify_competitions", "notify_announcements") if k in data}
        if dd:
            await self._write(_T["discord_data"], uid, dd, defaults=_DD_DEFAULTS)

        # Club-side state -> members (+ memberships for role/cell).
        m_fields: dict = {}
        if "club_id" in data:
            value = str(data["club_id"] or "").strip()
            m_fields["auth_user_id"] = value or None
        if "bio" in data:
            m_fields["bio"] = data["bio"] or None
        if "image_url" in data:
            m_fields["image_url"] = data["image_url"] or None
        needs_members = bool(m_fields) or \
            "club_role" in data or "cell" in data
        if needs_members:
            await self._touch_membership(uid, data, m_fields)

        # Freetext mirrors with no hub column -> bot_settings sidecar.
        side: dict = {}
        if "links" in data and data["links"] is not None:
            side["links"] = data["links"]
        if "mc_username" in data:
            side["mc_username"] = data["mc_username"]
        if side:
            await self._write_sidecar(uid, **side)

    async def _touch_membership(self, uid: str, data: dict,
                                m_fields: dict) -> None:
        du = await self._get(_T["discord_users"], uid)
        name = (du or {}).get("username") or uid
        if m_fields:
            await self._write(_T["members"], uid, m_fields,
                              defaults={"name": name,
                                        "created_at": _now_iso(),
                                        "updated_at": _now_iso()})
        if "club_role" in data or "cell" in data:
            # memberships.member references members — make the stub first.
            await self._write(_T["members"], uid, {},
                              defaults={"name": name,
                                        "created_at": _now_iso(),
                                        "updated_at": _now_iso()})
            mship: dict = {}
            if "club_role" in data:
                role = str(data.get("club_role") or "").strip().lower()
                mship["role"] = role if role in _KNOWN_ROLES else None
            if "cell" in data:
                cell = str(data.get("cell") or "").strip()
                mship["department"] = await self._resolve_department(cell)
                await self._write_sidecar(uid, cell=cell)
            await self._write(_T["memberships"], uid, mship,
                              defaults={"created_at": _now_iso()})

    async def increment_member(self, user_id: int, field: str,
                               amount: int | float = 1, *,
                               bootstrap: dict | None = None) -> None:
        """Legacy-compatible increment.

        TablesDB has no atomic increments. The production caller (warnings)
        appends a point-in-time ``warnings`` row; numeric discord_data fields
        are increment via read-modify-write (single-process bot, flushes are
        serialized by their own loop).
        """
        uid = str(user_id)
        if field == "warnings":
            username = dict(bootstrap or {}).get("username") or ""
            if username:
                await self._write(_T["discord_users"], uid,
                                  {"username": username},
                                  defaults={"username": username})
            name = username or uid
            await self._write(_T["members"], uid, {},
                              defaults={"name": name,
                                        "created_at": _now_iso(),
                                        "updated_at": _now_iso()})
            await self._create(_T["warnings"], ID.unique(), {
                # hub enum: card_type ∈ (yellow, red) — the bot's warn maps
                # to a formal yellow card for the member who was warned.
                "card_type": "yellow",
                "member": uid,
                "issued_at": _now_iso(),
            })
            return
        if field not in ("xp", "messages", "voice_seconds"):
            raise StoreError(f"increment_member: unsupported field {field!r}")
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return
        cur = await self._get(_T["discord_data"], uid)
        value = int((cur or {}).get(field) or 0) + amount
        await self._write(_T["discord_data"], uid, {field: value},
                          defaults=_DD_DEFAULTS)

    async def flush_member_activity(self, activity: dict) -> None:
        """Apply aggregated per-member deltas onto discord_data.

        ``activity[uid]`` maps column -> delta (xp/messages/voice_seconds),
        optionally carrying ``_bootstrap={"username": ...}`` for lazy row
        creation. Deltas accumulate on top of what the store already holds.
        """
        for user_id, delta in activity.items():
            fields = {k: v for k, v in delta.items()
                      if k in ("xp", "messages", "voice_seconds")}
            if not fields:
                continue
            uid = str(user_id)
            bootstrap = dict(delta.get("_bootstrap") or {})
            username = bootstrap.get("username") or ""
            if username:
                await self._write(_T["discord_users"], uid,
                                  {"username": username},
                                  defaults={"username": username})
            cur = await self._get(_T["discord_data"], uid)
            payload = dict(cur or _DD_DEFAULTS)
            for key, amount in fields.items():
                try:
                    if key == "voice_seconds":
                        payload[key] = int(round(
                            float(payload.get(key) or 0) + float(amount)))
                    else:
                        payload[key] = int(payload.get(key) or 0) + int(amount)
                except (TypeError, ValueError):
                    continue
            await self._write(_T["discord_data"], uid,
                              {k: payload[k] for k in fields},
                              defaults=_DD_DEFAULTS)

    async def list_members(self, *, limit: int = 100,
                           order_by: str = "messages") -> list[dict]:
        """Flat member records, sorted desc by a discord_data numeric column.

        Bulk path: pages discord_data (the only real source of the sortable
        stats) and batch-fills the identity/membership rows so 100 members
        cost a handful of requests instead of hundreds.
        """
        field = order_by if order_by in ("xp", "messages", "voice_seconds") \
            else "messages"
        rows = await self._listed(_T["discord_data"], min(limit, 500),
                                  order_by=field)
        uids = [r["$id"] for r in rows]
        dusers = await self._get_rows_batch(_T["discord_users"], uids)
        mems = await self._get_rows_batch(_T["members"], uids)
        mships = await self._get_rows_batch(_T["memberships"], uids)
        sides = await self._sidecar_batch(uids)
        out = []
        for row in rows:
            uid = row["$id"]
            out.append(await self._assemble(
                uid, dusers.get(uid), row, mships.get(uid), mems.get(uid),
                sides.get(uid)))
        return out

    # ── global counters (derived) ─────────────────────────────
    async def get_counters(self) -> dict:
        """Global totals derived by summing discord_data + settings stamps."""
        boot = (await self.get_setting("boot_at")) or ""
        last = (await self.get_setting("last_flush_at")) or ""
        total_messages = 0
        total_voice = 0
        offset = 0
        while True:
            page = await self._list(
                _T["discord_data"], [Query.limit(25), Query.offset(offset)])
            for row in page:
                total_messages += int(row.get("messages") or 0)
                total_voice += int(row.get("voice_seconds") or 0)
            if len(page) < 25:
                break
            offset += 25
        return {
            "total_messages": total_messages,
            "total_voice_seconds": total_voice,
            "boot_at": boot,
            "last_flush_at": last,
        }

    async def bump_counters(self, *, messages: int = 0,
                            voice_seconds: float = 0.0) -> None:
        """Stamp the flush marker (totals are derived, not stored)."""
        now = _now_iso()
        if (await self.get_setting("boot_at")) is None:
            await self.set_setting("boot_at", now)
        await self.set_setting("last_flush_at", now)

    async def set_counters(self, data: dict) -> None:
        # Legacy no-op: totals are summed from discord_data in this backend.
        now = _now_iso()
        await self.bump_counters()
        if (await self.get_setting("boot_at")) is None:
            await self.set_setting("boot_at", data.get("boot_at") or now)

    # ── daily challenges (re-homed in bot_settings) ───────────
    async def get_challenge(self, date: str) -> dict | None:
        raw = await self.get_setting(f"challenge.{date}")
        if not raw:
            return None
        value = _from_json(raw, None)
        return value if isinstance(value, dict) else None

    async def save_challenge(self, date: str, *, title: str,
                             description: str = "", created_by: str = "") -> None:
        current = (await self.get_challenge(date)) or {}
        doc = {
            "date": date,
            "title": title,
            "description": description,
            "created_by": str(created_by),
            "claimed": list(current.get("claimed") or []),
        }
        await self.set_setting(f"challenge.{date}",
                               json.dumps(doc, ensure_ascii=False))
        await self._index_challenge(date)

    async def _index_challenge(self, date: str) -> None:
        dates = await self._challenge_dates()
        if date not in dates:
            dates.append(date)
            await self.set_setting("challenges", json.dumps(dates))

    async def _challenge_dates(self) -> list[str]:
        raw = await self.get_setting("challenges")
        if not raw:
            return []
        value = _from_json(raw, [])
        return [str(d) for d in value if isinstance(value, list) and d]

    async def claim_challenge(self, date: str, user_id: int) -> bool:
        doc = await self.get_challenge(date)
        if not doc:
            raise StoreError("No challenge has been set for this date")
        claimed = list(doc.get("claimed") or [])
        uid = str(user_id)
        if uid in claimed:
            return False
        claimed.append(uid)
        doc["claimed"] = claimed
        await self.set_setting(f"challenge.{date}",
                               json.dumps(doc, ensure_ascii=False))
        return True

    async def clear_challenge(self, date: str) -> None:
        doc = (await self.get_challenge(date)) or {}
        doc["claimed"] = []
        await self.set_setting(f"challenge.{date}",
                               json.dumps(doc, ensure_ascii=False))

    async def list_recent_challenges(self, limit: int = 7) -> list[dict]:
        dates = sorted(await self._challenge_dates(), reverse=True)[:limit]
        out = []
        for date in dates:
            doc = await self.get_challenge(date)
            if doc:
                out.append(doc)
        return out

    # ── moderation log ────────────────────────────────────────
    async def log_moderation(self, *, action: str, target_id: int,
                             target_name: str = "", moderator_id: int | None = None,
                             moderator_name: str = "", reason: str = "") -> None:
        """Record a moderation action.

        The hub modlog stores action / target_id / reason / created_at only;
        target_name and moderator fields have no column and are dropped — the
        renderer already falls back to ``<@target_id>`` and "staff".
        """
        await self._create(_T["modlog"], ID.unique(), {
            "action": str(action or "?")[:64],
            "target_id": str(target_id) if target_id else None,
            "reason": reason or None,
            "created_at": _now_iso(),
        })

    async def list_modlog(self, limit: int = 20) -> list[dict]:
        return await self._listed(_T["modlog"], limit, order_by="created_at")

    # ── tasks ─────────────────────────────────────────────────
    # The hub enum-constrains status to (todo, in_progress, done, blocked);
    # the bot's vocabulary is (open, in_progress, done, cancelled). The hub
    # column stores a valid value while the exact bot string round-trips in
    # the task sidecar so every cog read/filter behaves unchanged.
    _TASK_STATUS_HUB = {"open": "todo", "cancelled": "blocked"}
    _TASK_STATUS_BOT = {"todo": "open", "blocked": "cancelled"}

    @classmethod
    def _task_out(cls, row: dict, cell: str = "", status_raw: str = "") -> dict:
        status = status_raw or cls._TASK_STATUS_BOT.get(
            row.get("status") or "", row.get("status") or "open")
        return {
            "$id": row["$id"],
            "task_id": row["$id"],
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "priority": row.get("priority") or "medium",
            "status": status,
            # deadline (hub) round-trips to the legacy "YYYY-MM-DD" due.
            "due": _iso_text(row.get("deadline"))[:10] or "",
            "cell": cell,
            "assignee": _rel_id(row.get("assignee")),
            "created_by": _rel_id(row.get("created_by")),
            "parent_task": _rel_id(row.get("parent_task")),
        }

    async def _task_sidecar(self, task_id: str) -> dict:
        raw = await self.get_setting(f"task_cell.{task_id}")
        side = _from_json(raw, {})
        return side if isinstance(side, dict) else {}

    async def list_tasks(self) -> list[dict]:
        rows = await self._listed(_T["tasks"], 100)
        side = await self._batch_settings([f"task_cell.{r['$id']}" for r in rows])
        out = []
        for r in rows:
            meta = _from_json(side.get(f"task_cell.{r['$id']}"), {}) or {}
            out.append(self._task_out(r, meta.get("cell") or "",
                                      str(meta.get("status") or "")))
        return out

    async def get_task(self, task_id: str) -> dict | None:
        row = await self._get(_T["tasks"], task_id)
        if row is None:
            return None
        meta = await self._task_sidecar(task_id)
        return self._task_out(row, meta.get("cell") or "",
                              str(meta.get("status") or ""))

    async def save_task(self, payload: dict) -> None:
        task_id = payload.get("task_id")
        if not task_id:
            raise StoreError("save_task requires a task_id")
        status_raw = str(payload.get("status") or "open")[:32]
        data = {
            "title": str(payload.get("title") or "")[:256],
            "description": payload.get("description") or None,
            "status": self._TASK_STATUS_HUB.get(status_raw, status_raw),
            "priority": str(payload.get("priority") or "medium")[:16],
            "parent_task": payload.get("parent_task") or None,
            "source": "bot",
            "deadline": _to_iso_datetime(payload.get("due")),
        }
        assignee = str(payload.get("assignee") or "").strip()
        created_by = str(payload.get("created_by") or "").strip()
        data["assignee"] = assignee or None
        data["created_by"] = created_by or None
        if assignee:
            await self._ensure_members_row(assignee)
        if created_by:
            await self._ensure_members_row(created_by)
        # Cell is free text with no departments row: keep the hub FK off and
        # mirror the label (and the exact bot status) in a sidecar so task
        # embeds and filters render them unchanged.
        await self.set_setting(
            f"task_cell.{task_id}",
            json.dumps({"cell": str(payload.get("cell") or ""),
                        "status": status_raw}))
        await self._write(_T["tasks"], task_id, data,
                          defaults={"title": data["title"],
                                    "status": data["status"],
                                    "priority": data["priority"],
                                    "source": "bot"})

    async def _ensure_members_row(self, member_id: str) -> str:
        """Lazily create the members stub a club-state FK points at."""
        if not member_id:
            return member_id
        row = await self._get(_T["members"], member_id)
        if row is None:
            await self._create(_T["members"], member_id, {
                "name": member_id,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            })
        return member_id

    async def next_task_code(self) -> str:
        """Next human-friendly task id, e.g. T-13 (skips existing codes)."""
        rows = await self._listed(_T["tasks"], 100)
        used = {str(r["$id"]) for r in rows}
        n = len(used) + 1
        code = f"T-{n}"
        while code in used:
            n += 1
            code = f"T-{n}"
        return code

    # ── competitions ──────────────────────────────────────────
    @staticmethod
    def _competition_out(row: dict) -> dict:
        return {
            "$id": row["$id"],
            "slug": row["$id"],
            "name": row.get("name") or "",
            "date": _event_date_out(row.get("date")),
            "location": "",          # no hub column — embed falls back to TBD
            "description": "",
            "capacity": row.get("capacity"),
            "registered": _from_json(row.get("registered")) or [],
            "created_by": _rel_id(row.get("created_by")),
        }

    async def list_competitions(self) -> list[dict]:
        rows = await self._listed(_T["competitions"], 100)
        return [self._competition_out(r) for r in rows]

    async def get_competition(self, slug: str) -> dict | None:
        row = await self._competition_row(slug)
        if row is None:
            return None
        return self._competition_out(row)

    async def _competition_row(self, slug: str) -> dict | None:
        row = await self._get(_T["competitions"], slug)
        if row is not None:
            return row
        mapped = await self.get_setting(f"slugmap.{_short_hash(slug)}")
        if mapped and mapped != slug:
            row = await self._get(_T["competitions"], mapped)
        return row

    async def save_competition(self, slug: str, payload: dict) -> None:
        row_id, mapped = self._slug_row_id(slug)
        created_by = str(payload.get("created_by") or "").strip()
        if created_by:
            await self._ensure_members_row(created_by)
        data = {
            "name": str(payload.get("name") or "")[:256],
            "date": _event_date_in(payload.get("date")),
            "capacity": payload.get("capacity"),
            "registered": _as_json(payload.get("registered") or []),
            "source": "bot",
            "created_by": created_by or None,
        }
        if mapped:
            await self.set_setting(f"slugmap.{_short_hash(slug)}", row_id)
        await self._write(_T["competitions"], row_id, data,
                          defaults={"name": data["name"],
                                    "date": data["date"],
                                    "source": "bot"})

    async def set_registration(self, slug: str, user_id: int,
                               registered: bool) -> tuple[bool, str]:
        """Register/unregister for a competition. Returns (ok, message)."""
        doc = await self.get_competition(slug)
        if not doc:
            raise StoreError("Competition not found")
        regs = list(doc.get("registered") or [])
        sid = str(user_id)
        if registered:
            if sid in regs:
                return False, "already registered"
            capacity = doc.get("capacity")
            if capacity and len(regs) >= int(capacity):
                return False, "full"
            regs.append(sid)
            action = "registered"
        else:
            if sid not in regs:
                return False, "not registered"
            regs.remove(sid)
            action = "registration removed"
        doc["registered"] = regs
        await self.save_competition(slug, doc)
        return True, action

    # ── events ────────────────────────────────────────────────
    @staticmethod
    def _event_out(row: dict) -> dict:
        return {
            "$id": row["$id"],
            "slug": row["$id"],
            "title": row.get("title") or "",
            "date": _event_date_out(row.get("date")),
            "time": row.get("time") or "",
            "location": "",          # no hub column — embed falls back to TBD
            "description": "",
            "attendees": _from_json(row.get("attendees")) or [],
            "declined": _from_json(row.get("declined")) or [],
            "created_by": _rel_id(row.get("created_by")),
        }

    async def list_events(self) -> list[dict]:
        rows = await self._listed(_T["events"], 100)
        return [self._event_out(r) for r in rows]

    async def get_event(self, slug: str) -> dict | None:
        row = await self._event_row(slug)
        if row is None:
            return None
        return self._event_out(row)

    async def _event_row(self, slug: str) -> dict | None:
        row = await self._get(_T["events"], slug)
        if row is not None:
            return row
        mapped = await self.get_setting(f"slugmap.{_short_hash(slug)}")
        if mapped and mapped != slug:
            row = await self._get(_T["events"], mapped)
        return row

    @staticmethod
    def _slug_row_id(slug: str) -> tuple[str, bool]:
        """Hub row ids cap at 36 chars — truncate and remember the mapping."""
        if len(slug) <= 36:
            return slug, False
        return slug[:36], True

    async def save_event(self, slug: str, payload: dict) -> None:
        row_id, mapped = self._slug_row_id(slug)
        created_by = str(payload.get("created_by") or "").strip()
        if created_by:
            await self._ensure_members_row(created_by)
        data = {
            "title": str(payload.get("title") or "")[:256],
            "date": _event_date_in(payload.get("date")),
            "time": str(payload.get("time") or "")[:16] or None,
            "attendees": _as_json(payload.get("attendees") or []),
            "declined": _as_json(payload.get("declined") or []),
            "source": "bot",
            "created_by": created_by or None,
        }
        if mapped:
            await self.set_setting(f"slugmap.{_short_hash(slug)}", row_id)
        await self._write(_T["events"], row_id, data,
                          defaults={"title": data["title"],
                                    "date": data["date"],
                                    "source": "bot"})

    async def set_rsvp(self, slug: str, user_id: int,
                       attending: bool | None) -> str:
        """Set attendance (True/False) or clear it (None). Returns a label."""
        doc = await self.get_event(slug)
        if not doc:
            raise StoreError("Event not found")
        sid = str(user_id)
        attendees = [x for x in (doc.get("attendees") or []) if x != sid]
        declined = [x for x in (doc.get("declined") or []) if x != sid]
        if attending is True:
            attendees.append(sid)
            label = "✅ marked as attending"
        elif attending is False:
            declined.append(sid)
            label = "❌ marked as not attending"
        else:
            label = "❔ RSVP cleared"
        doc["attendees"] = attendees
        doc["declined"] = declined
        await self.save_event(slug, doc)
        return label

    # ── polls ─────────────────────────────────────────────────
    @staticmethod
    def _poll_out(row: dict) -> dict:
        return {
            "$id": row["$id"],
            "poll_id": row["$id"],
            "question": row.get("question") or "",
            "options": _from_json(row.get("options")) or [],
            "mode": row.get("mode") or "transparent",
            "selection": row.get("selection") or "single",
            "hide_results": bool(row.get("hide_results")),
            "closed": bool(row.get("closed")),
            "votes": _from_json(row.get("votes")) or [],
            "created_by": _rel_id(row.get("created_by")),
            # No hub columns for these — legacy timestamps are not ported.
            "created_at": "",
            "closed_at": None,
            "source": row.get("source") or "bot",
        }

    async def list_polls(self, limit: int = 100) -> list[dict]:
        # No created_at column: the legacy newest-first order is not
        # representable, so rows come back in natural table order.
        rows = await self._listed(_T["polls"], limit)
        return [self._poll_out(r) for r in rows]

    async def get_poll(self, poll_id: str) -> dict | None:
        row = await self._get(_T["polls"], poll_id)
        if row is None:
            return None
        return self._poll_out(row)

    async def save_poll(self, poll_id: str, payload: dict) -> None:
        created_by = str(payload.get("created_by") or "").strip()
        if created_by:
            await self._ensure_members_row(created_by)
        data = {
            "question": str(payload.get("question") or "")[:256],
            "options": _as_json(payload.get("options") or []),
            "mode": str(payload.get("mode") or "transparent")[:16],
            "selection": str(payload.get("selection") or "single")[:64],
            "hide_results": bool(payload.get("hide_results", False)),
            "closed": bool(payload.get("closed", False)),
            "votes": _as_json(payload.get("votes") or []),
            "source": "bot",
            "created_by": created_by or None,
        }
        await self._write(_T["polls"], poll_id, data,
                          defaults={"question": data["question"],
                                    "hide_results": data["hide_results"],
                                    "closed": data["closed"],
                                    "source": "bot"})

    async def next_poll_code(self) -> str:
        """Next human-friendly poll id, e.g. P-7 (skips existing ids)."""
        rows = await self._listed(_T["polls"], 100)
        used = {str(r["$id"]) for r in rows}
        n = len(used) + 1
        code = f"P-{n}"
        while code in used:
            n += 1
            code = f"P-{n}"
        return code

    # ── Minecraft login (OTP-only) + links ───────────────────
    # OTP-only contract (mc-link plugin, MITIGATION-PLAN §5):
    #   * /mclink mints an ACCOUNT row + a pending PAIR row (pair_key); the
    #     plugin claims it in-game (/mcverify) by flipping is_active.
    #   * The PLUGIN arms minecraft_otp rows as "pending mint" (enabled with
    #     an empty otp_hash) at join; this bot mints a bcrypt-12 hash into
    #     them and DMs the code. The plugin verifies and consumes.
    async def mc_find_account(self, username: str) -> dict | None:
        """The minecraft_accounts row for an exact username (unique index)."""
        rows = await self._listed(
            _T["minecraft_accounts"], 25,
            queries=[Query.equal("username", username)])
        return rows[0] if rows else None

    async def mc_resolve_account(self, account_id: str) -> dict | None:
        """Fetch a minecraft_accounts row by $id, or None."""
        if not account_id:
            return None
        tdb, db_id = self._raw()
        try:
            row = await asyncio.to_thread(
                tdb.get_row, db_id, _T["minecraft_accounts"], account_id)
        except AppwriteException as exc:
            if is_missing(exc):
                return None
            raise StoreError(f"resolve account {account_id}: {exc}") from exc
        return self._row_data(row)

    async def mc_account_linked(self, username: str) -> bool:
        """True when an active link points at this account (name taken)."""
        account = await self.mc_find_account(username)
        if not account:
            return False
        rows = await self._listed(
            _T["discord_mc_links"], 25,
            queries=[Query.equal("minecraft_account", account["$id"]),
                     Query.equal("is_active", True)])
        return bool(rows)

    async def mc_user_linked(self, discord_id: int) -> bool:
        """True when this Discord user already holds an active link (the
        "one active link per Discord user" contract rule)."""
        rows = await self._listed(
            _T["discord_mc_links"], 25,
            queries=[Query.equal("discord_user", str(discord_id)),
                     Query.equal("is_active", True)])
        return bool(rows)

    async def mc_create_pair(self, discord_id: int, username: str,
                             pair_key: str, *, username_hint: str = "") -> str:
        """/mclink: upsert the account + the Discord identity, then create the
        pending pair row.

        ``minecraft_accounts.$id == username`` with the exact casing the
        member typed — the plugin compares the in-game name against this
        value exactly. The ``discord_users`` row must exist too: TablesDB
        silently NULLs a relationship column on PATCH when the related
        document is missing, and the plugin's claim patch touches the link
        row. Returns the link row id so the caller can drop it if the DM
        fails; the watcher expires unclaimed rows after 5 minutes.
        """
        uid = str(discord_id)
        await self._ensure_discord_identity(uid, username_hint)
        await self._write(
            _T["minecraft_accounts"], username,
            {"username": username, "is_cracked": True},
            defaults={"username": username, "is_cracked": True})
        return await self._create(_T["discord_mc_links"], ID.unique(), {
            "pair_key": pair_key,
            "is_active": False,
            "verified_at": None,
            "discord_user": uid,
            "minecraft_account": username,
        })

    async def _ensure_discord_identity(self, uid: str,
                                       username: str = "") -> None:
        """Create the discord_users row only when it's absent — the ``discord
        _user`` FK of a link row silently dies on later PATCHes when its
        related document is missing. Never overwrites an existing profile."""
        if await self._get(_T["discord_users"], uid) is None:
            await self._create(_T["discord_users"], uid,
                               {"username": username or ""})

    async def mc_delete_pair(self, link_id: str) -> None:
        """Drop an unclaimed pairing row (DM failure or stale expiry)."""
        if not link_id:
            return
        tdb, db_id = self._raw()
        try:
            await asyncio.to_thread(tdb.delete_row, db_id,
                                    _T["discord_mc_links"], link_id)
        except AppwriteException as exc:
            if not is_missing(exc):
                raise StoreError(f"expire pair {link_id}: {exc}") from exc

    async def mc_stale_pairs(self, older_than: datetime) -> list[dict]:
        """Unclaimed (is_active=False) pair rows created before ``older_than``."""
        rows = await self._listed(
            _T["discord_mc_links"], 100,
            queries=[Query.equal("is_active", False)])
        out = []
        for row in rows:
            created = _parse_ts(row.get("$createdAt"))
            if created is not None and created < older_than:
                out.append(row)
        return out

    # ── OTP minting ──────────────────────────────────────────
    async def mc_list_pending_otps(self) -> list[dict]:
        """minecraft_otp rows the plugin armed but no code is minted for yet
        (enabled with an empty otp_hash — the "pending mint" state)."""
        rows = await self._listed(
            _T["minecraft_otp"], 100,
            queries=[Query.equal("enabled", True)])
        return [r for r in rows if not str(r.get("otp_hash") or "")]

    async def mc_mint_otp(self, row_id: str, *, otp_hash: str, otp_salt: str,
                          challenge_at: str, expires_at: str) -> bool:
        """Fill a pending OTP row with a minted hash. False when the row is
        gone, consumed/locked, or already minted — a double-watch race guard;
        a minted row is never re-minted."""
        tdb, db_id = self._raw()
        try:
            row = await asyncio.to_thread(
                tdb.get_row, db_id, _T["minecraft_otp"], row_id)
        except AppwriteException as exc:
            if is_missing(exc):
                return False
            raise StoreError(f"mint otp {row_id}: {exc}") from exc
        data = self._row_data(row)
        if not data.get("enabled") or str(data.get("otp_hash") or ""):
            return False
        await self._patch(_T["minecraft_otp"], row_id, {
            "otp_hash": otp_hash,
            "otp_salt": otp_salt,
            "challenge_at": challenge_at,
            "expires_at": expires_at,
            "failed_attempts": 0,
        })
        return True

    async def mc_account_otp_owner(self, account_id: str
                                   ) -> tuple[str, int] | None:
        """(username, discord_id) entitled to a minted code, when the account
        exists AND an active link binds it to a Discord user."""
        account = await self.mc_resolve_account(account_id)
        if not account:
            return None
        links = await self._listed(
            _T["discord_mc_links"], 25,
            queries=[Query.equal("minecraft_account", account_id),
                     Query.equal("is_active", True)])
        for link in links:
            discord_id = _rel_id(link.get("discord_user"))
            if discord_id:
                return account.get("username") or "", int(discord_id or 0)
        return None

    async def mc_expire_otp(self, otp_id: str) -> None:
        """Disable an OTP (DM delivery failed — the plugin re-arms on rejoin)."""
        if not otp_id:
            return
        await self._patch(_T["minecraft_otp"], otp_id, {"enabled": False})

    async def mc_list_active_links(self) -> list[dict]:
        """Every active discord_mc_links row (the watcher's sync queue)."""
        return await self._listed(
            _T["discord_mc_links"], 100,
            queries=[Query.equal("is_active", True)])

    async def mc_deactivate_link(self, discord_id: int,
                                 username: str | None = None) -> None:
        """Flip the member's link row(s) back to inactive (unlink flow).

        TablesDB forbids bulk updates on tables carrying relationship
        attributes, so matching rows are listed (queries are fine) and each
        row is patched individually. ``username`` resolves to the account id
        (pair_key is now a code, not the username).
        """
        queries = [Query.equal("discord_user", str(discord_id))]
        if username:
            account = await self.mc_find_account(username)
            if not account:
                return  # nothing to deactivate — account never existed
            queries.append(Query.equal("minecraft_account", account["$id"]))
        rows = await self._listed(_T["discord_mc_links"], 100, queries=queries)
        for row in rows:
            await self._patch(_T["discord_mc_links"], row["$id"],
                              {"is_active": False})

    async def mc_upsert_link(self, discord_id: int, username: str, *,
                             verified_at: str | None = None,
                             account_uuid: str | None = None,
                             is_cracked: bool = True) -> None:
        """Record a verified link (whitelist flow): link row + account row.

        ``minecraft_accounts.$id == username``; ``discord_mc_links`` rows are
        matched by (discord_user, minecraft_account) so re-links update in
        place and pairing-row FKs survive.
        """
        uid = str(discord_id)
        if verified_at is None:
            verified_at = _now_iso()
        # Related documents must exist: TablesDB NULLs the link's FK columns
        # on PATCH when ``discord_users`` or ``minecraft_accounts`` is absent.
        await self._ensure_discord_identity(uid)
        # Account row first (the link's minecraft_account FK references it).
        await self._write(
            _T["minecraft_accounts"], username,
            {"username": username,
             "uuid": account_uuid or "",
             "is_cracked": bool(is_cracked)},
            defaults={"username": username,
                      "is_cracked": bool(is_cracked)})
        existing = await self._listed(
            _T["discord_mc_links"], 25,
            queries=[Query.equal("discord_user", uid),
                     Query.equal("minecraft_account", username)])
        if existing:
            link_id = existing[0]["$id"]
            await self._write(
                _T["discord_mc_links"], link_id,
                {"is_active": True,
                 "verified_at": verified_at,
                 "minecraft_account": username},
                defaults={"pair_key": username, "is_active": True})
        else:
            await self._write(
                _T["discord_mc_links"], ID.unique(),
                {"pair_key": username,
                 "is_active": True,
                 "verified_at": verified_at,
                 "discord_user": uid,
                 "minecraft_account": username},
                defaults={"pair_key": username, "is_active": True})


store = Store()