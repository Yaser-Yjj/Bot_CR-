"""Async, typed access to Bot_CR's Appwrite collections.

The Appwrite Python SDK is synchronous and blocking, so every call is
dispatched through ``asyncio.to_thread`` to keep the Discord gateway loop
responsive. High-frequency events (messages, voice time) are accumulated
in-memory by the stats/engagement cogs and flushed periodically through
``flush_member_activity`` / ``bump_counters``, so writes stay in the
single-digits-per-minute range even on a busy server.
"""

import asyncio
import logging
from datetime import datetime, timezone

from appwrite.exception import AppwriteException
from appwrite.id import ID
from appwrite.query import Query

from data.appwrite_client import build_databases, ensure_schema as _ensure_schema_sync, is_missing

LOG = logging.getLogger("bot.store")

# Collection id shorthand -> actual Appwrite collection id.
COLL = {
    "members": "bot_members",
    "counters": "bot_counters",
    "challenges": "bot_challenges",
    "modlog": "bot_modlog",
    "settings": "bot_settings",
    "tasks": "bot_tasks",
    "competitions": "bot_competitions",
    "events": "bot_events",
    "polls": "bot_polls",
    # mc-link (Discord ↔ Minecraft single sign-on).
    "mc_codes": "mc_link_codes",
    "mc_auth": "mc_auth",
    "mc_challenges": "mc_challenges",
}


class StoreError(RuntimeError):
    """Raised when an Appwrite operation fails."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filled(data: dict) -> dict:
    """Drop None values so they never clobber existing stored data."""
    return {k: v for k, v in data.items() if v is not None}


class Store:
    """Typed facade over the Appwrite Databases service."""

    def __init__(self):
        self._db = None
        self._db_id = None

    # ── lifecycle ──────────────────────────────────────────────
    async def init(self) -> None:
        """Connect and (idempotently) ensure the schema exists."""
        self._db = build_databases()
        from config import APPWRITE_DATABASE_ID
        self._db_id = APPWRITE_DATABASE_ID
        await asyncio.to_thread(_ensure_schema_sync, self._db)
        LOG.info("Appwrite store ready (db=%s)", self._db_id)

    async def ensure_schema(self) -> None:
        await asyncio.to_thread(_ensure_schema_sync, self._db or build_databases())

    def _raw(self):
        if self._db is None or self._db_id is None:
            raise StoreError("Store not initialised — call await store.init() first")
        return self._db, self._db_id

    # ── low-level helpers ──────────────────────────────────────
    @staticmethod
    def _doc_data(doc) -> dict | None:
        """Extract the plain data dict from an SDK Document model.

        Appwrite keeps ``$id``/``$createdAt``/``$updatedAt`` as document meta
        (outside the ``data`` payload); those are surfaced here so callers can
        address docs by id and resume watchers from the last update marker.
        """
        if doc is None:
            return None
        raw = doc.to_dict()
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            data = dict(raw["data"])
        else:
            data = dict(raw)
        doc_id = raw.get("$id") or data.get("$id")
        if doc_id:
            data["$id"] = doc_id
        updated = raw.get("$updatedAt")
        if hasattr(updated, "isoformat"):
            updated = updated.isoformat()
        if updated:
            data["$updatedAt"] = updated
        return data

    async def _get(self, collection: str, doc_id: str) -> dict | None:
        db, db_id = self._raw()
        try:
            doc = await asyncio.to_thread(db.get_document, db_id, collection, doc_id)
        except AppwriteException as exc:
            if is_missing(exc):
                return None
            raise StoreError(f"get {collection}/{doc_id}: {exc}") from exc
        return self._doc_data(doc)

    async def _replace(self, collection: str, doc_id: str, data: dict) -> None:
        """Full-document upsert (replaces the whole doc)."""
        db, db_id = self._raw()
        try:
            await asyncio.to_thread(db.upsert_document, db_id, collection, doc_id, data)
        except AppwriteException as exc:
            raise StoreError(f"replace {collection}/{doc_id}: {exc}") from exc

    async def _patch(self, collection: str, doc_id: str, data: dict) -> None:
        """Partial update: merges non-None fields into the existing doc."""
        existing = (await self._get(collection, doc_id)) or {}
        existing.update(_filled(data))
        await self._replace(collection, doc_id, existing)

    async def _create(self, collection: str, doc_id: str, data: dict) -> str | None:
        """Create a document; returns its ``$id`` (random ids: pass ``ID.unique()``)."""
        db, db_id = self._raw()
        try:
            doc = await asyncio.to_thread(db.create_document, db_id, collection, doc_id, data)
        except AppwriteException as exc:
            raise StoreError(f"create {collection}/{doc_id}: {exc}") from exc
        if doc is None:
            return None
        raw = doc.to_dict() if isinstance(doc.to_dict(), dict) else {}
        return raw.get("$id") or (raw.get("data") or {}).get("$id")

    async def _list(self, collection: str, queries: list[str] | None = None,
                    limit: int = 100) -> list[dict]:
        db, db_id = self._raw()
        queries = [Query.limit(limit)] + list(queries or [])
        try:
            result = await asyncio.to_thread(db.list_documents, db_id, collection, queries)
        except AppwriteException as exc:
            raise StoreError(f"list {collection}: {exc}") from exc
        docs = getattr(result, "documents", None) or []
        return [self._doc_data(d) for d in docs if d is not None]

    # ── members ────────────────────────────────────────────────
    async def get_member(self, user_id: int) -> dict | None:
        return await self._get(COLL["members"], str(user_id))

    async def merge_member(self, user_id: int, data: dict) -> None:
        """Create-or-update a member doc, merging in only the given fields."""
        data = dict(data)
        data["user_id"] = str(user_id)
        await self._patch(COLL["members"], str(user_id), data)

    async def increment_member(self, user_id: int, field: str, amount: int | float,
                               *, bootstrap: dict | None = None) -> None:
        """Atomic server-side increment of one numeric member field.

        If the member doc is missing and ``bootstrap`` is provided, the doc is
        created first (with that data) so the increment can apply. Deltas must
        be positive — Appwrite rejects zero/negative increments — and a doc
        whose field is null (legacy/dashboard-created) is normalised to 0
        before the increment so the delta still lands.
        """
        if not isinstance(amount, (int, float)) or amount <= 0:
            return
        db, db_id = self._raw()
        doc_id = str(user_id)

        async def _run() -> AppwriteException | None:
            try:
                await asyncio.to_thread(
                    db.increment_document_attribute,
                    db_id, COLL["members"], doc_id, field, amount,
                )
                return None
            except AppwriteException as exc:
                return exc

        exc = await _run()
        if exc is None:
            return
        if "not a number" in str(exc):
            # Doc exists but the field is null/empty — normalise to 0 once,
            # then apply the delta.
            await self._patch(COLL["members"], doc_id, {field: 0})
            exc = await _run()
            if exc is None:
                return
            raise StoreError(f"increment {field} for {user_id}: {exc}") from exc
        if not is_missing(exc):
            raise StoreError(f"increment {field} for {user_id}: {exc}") from exc
        if bootstrap is None:
            raise StoreError(f"increment {field} for {user_id}: member doc missing") from exc
        base = dict(bootstrap)
        base["user_id"] = str(user_id)
        base.setdefault(field, 0)
        await self._patch(COLL["members"], doc_id, base)
        exc = await _run()
        if exc is None:
            return
        raise StoreError(f"increment {field} for {user_id}: {exc}") from exc

    async def flush_member_activity(self, activity: dict[int, dict]) -> None:
        """Apply aggregated per-member deltas (one request per active user).

        Values are *deltas* — they accumulate via atomic server-side
        increments so historical totals are never clobbered. Member docs that
        are missing (legacy users who never onboarded) are bootstrapped with
        the touched field so the increment can apply.
        """
        for user_id, delta in activity.items():
            fields = {k: v for k, v in delta.items() if isinstance(v, (int, float))}
            if not fields:
                continue
            try:
                for field, amount in fields.items():
                    if field in ("user_id", "_bootstrap"):
                        continue
                    await self.increment_member(
                        user_id, field, amount, bootstrap={"username": "", field: 0}
                    )
            except StoreError as exc:
                LOG.error("Flush failed for member %s: %s", user_id, exc)

    async def list_members(self, *, limit: int = 100, order_by: str = "messages") -> list[dict]:
        return await self._list(COLL["members"], queries=[Query.order_desc(order_by)], limit=limit)

    # ── global counters ────────────────────────────────────────
    async def get_counters(self) -> dict:
        return (await self._get(COLL["counters"], "global")) or {}

    async def bump_counters(self, *, messages: int = 0, voice_seconds: float = 0.0) -> None:
        """Increment the global counters (atomic) and stamp the flush time.

        Only positive deltas are sent: Appwrite rejects zero-valued
        increments, so a flush with voice time but no messages (or the other
        way round) must not try to increment the empty counter.
        """
        db, db_id = self._raw()
        now = _now_iso()
        created = False
        try:
            for field, amount in (
                ("total_messages", messages),
                ("total_voice_seconds", round(voice_seconds, 1)),
            ):
                if not isinstance(amount, (int, float)) or amount <= 0:
                    continue
                try:
                    await asyncio.to_thread(
                        db.increment_document_attribute,
                        db_id, COLL["counters"], "global", field, amount,
                    )
                except AppwriteException as exc:
                    if "not a number" in str(exc):
                        # Counter doc exists but the value is null — normalise
                        # to 0 and retry once.
                        await asyncio.to_thread(
                            db.update_document,
                            db_id, COLL["counters"], "global", {field: 0},
                        )
                        await asyncio.to_thread(
                            db.increment_document_attribute,
                            db_id, COLL["counters"], "global", field, amount,
                        )
                    else:
                        raise
        except AppwriteException as exc:
            if not is_missing(exc):
                raise StoreError(f"bump counters: {exc}") from exc
            # Counter doc missing -> create it with the current deltas.
            await self._create(COLL["counters"], "global", {
                "total_messages": messages,
                "total_voice_seconds": float(voice_seconds),
                "boot_at": now,
                "last_flush_at": now,
            })
            created = True
        if not created:
            try:
                await asyncio.to_thread(
                    db.update_document, db_id, COLL["counters"], "global", {"last_flush_at": now}
                )
            except AppwriteException as exc:
                raise StoreError(f"stamp counters: {exc}") from exc

    async def set_counters(self, data: dict) -> None:
        await self._replace(COLL["counters"], "global", data)

    # ── daily challenges ───────────────────────────────────────
    async def get_challenge(self, date: str) -> dict | None:
        return await self._get(COLL["challenges"], date)

    async def save_challenge(self, date: str, *, title: str, description: str = "",
                             created_by: str = "") -> None:
        existing = (await self.get_challenge(date)) or {}
        payload = {
            "date": date,
            "title": title,
            "description": description,
            "created_by": created_by,
            "claimed": existing.get("claimed") or [],
        }
        await self._replace(COLL["challenges"], date, payload)

    async def claim_challenge(self, date: str, user_id: int) -> bool:
        """Claim today's challenge; True the first time this user claims it."""
        payload = (await self.get_challenge(date)) or {}
        if not payload.get("title"):
            raise StoreError("No challenge has been set for this date")
        claimed = list(payload.get("claimed") or [])
        sid = str(user_id)
        if sid in claimed:
            return False
        claimed.append(sid)
        payload["claimed"] = claimed
        await self._replace(COLL["challenges"], date, payload)
        return True

    async def clear_challenge(self, date: str) -> None:
        payload = (await self.get_challenge(date)) or {}
        payload["claimed"] = []
        await self._replace(COLL["challenges"], date, payload)

    async def list_recent_challenges(self, limit: int = 7) -> list[dict]:
        docs = await self._list(COLL["challenges"], queries=[Query.order_desc("date")], limit=100)
        docs.sort(key=lambda d: str(d.get("date", "")), reverse=True)
        return docs[:limit]

    # ── moderation log ─────────────────────────────────────────
    async def log_moderation(self, *, action: str, target_id: int, target_name: str = "",
                             moderator_id: int | None = None, moderator_name: str = "",
                             reason: str = "") -> None:
        payload = {
            "action": action,
            "target_id": str(target_id),
            "target_name": target_name,
            "moderator_id": str(moderator_id) if moderator_id else "",
            "moderator_name": moderator_name,
            "reason": reason,
            "created_at": _now_iso(),
        }
        await self._create(COLL["modlog"], ID.unique(), payload)

    async def list_modlog(self, limit: int = 20) -> list[dict]:
        return await self._list(COLL["modlog"], queries=[Query.order_desc("created_at")], limit=limit)

    # ── settings ───────────────────────────────────────────────
    async def get_setting(self, key: str) -> str | None:
        doc = await self._get(COLL["settings"], key)
        return (doc or {}).get("value")

    async def set_setting(self, key: str, value: str) -> None:
        await self._replace(COLL["settings"], key, {"value": value})

    # ── tasks ──────────────────────────────────────────────────
    async def list_tasks(self) -> list[dict]:
        """All tasks (club-scale lists fit Appwrite's 100-doc page limit)."""
        return await self._list(COLL["tasks"], limit=100)

    async def get_task(self, task_id: str) -> dict | None:
        return await self._get(COLL["tasks"], task_id)

    async def save_task(self, payload: dict) -> None:
        task_id = payload.get("task_id")
        if not task_id:
            raise StoreError("save_task requires a task_id")
        await self._replace(COLL["tasks"], task_id, payload)

    async def next_task_code(self) -> str:
        """Next human-friendly task code, e.g. T-13 (skips existing codes)."""
        docs = await self.list_tasks()
        used = {str(d.get("task_id", "")) for d in docs}
        n = len(used) + 1
        code = f"T-{n}"
        while code in used:
            n += 1
            code = f"T-{n}"
        return code

    # ── competitions ───────────────────────────────────────────
    async def list_competitions(self) -> list[dict]:
        return await self._list(COLL["competitions"], limit=100)

    async def get_competition(self, slug: str) -> dict | None:
        return await self._get(COLL["competitions"], slug)

    async def save_competition(self, slug: str, payload: dict) -> None:
        await self._replace(COLL["competitions"], slug, payload)

    async def set_registration(self, slug: str, user_id: int, registered: bool
                               ) -> tuple[bool, str]:
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

    # ── events ─────────────────────────────────────────────────
    async def list_events(self) -> list[dict]:
        return await self._list(COLL["events"], limit=100)

    async def get_event(self, slug: str) -> dict | None:
        return await self._get(COLL["events"], slug)

    async def save_event(self, slug: str, payload: dict) -> None:
        await self._replace(COLL["events"], slug, payload)

    async def set_rsvp(self, slug: str, user_id: int, attending: bool | None
                       ) -> str:
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
    async def list_polls(self, limit: int = 100) -> list[dict]:
        """All polls, newest first (club-scale fits the 100-doc page limit)."""
        return await self._list(
            COLL["polls"],
            queries=[Query.order_desc("created_at")],
            limit=limit,
        )

    async def get_poll(self, poll_id: str) -> dict | None:
        return await self._get(COLL["polls"], poll_id)

    async def save_poll(self, poll_id: str, payload: dict) -> None:
        """Full-document upsert (votes included)."""
        await self._replace(COLL["polls"], poll_id, payload)

    async def next_poll_code(self) -> str:
        """Next human-friendly poll id, e.g. P-7 (skips existing ids)."""
        docs = await self.list_polls()
        used = {str(d.get("poll_id", "")) for d in docs}
        n = len(used) + 1
        code = f"P-{n}"
        while code in used:
            n += 1
            code = f"P-{n}"
        return code

    # ── mc-link (Discord ↔ Minecraft single sign-on) ─────────
    async def mc_create(self, collection: str, data: dict) -> str:
        """Create a doc with a random id; returns its ``$id`` for later edits."""
        doc_id = await self._create(COLL[collection], ID.unique(), data)
        if not doc_id:
            raise StoreError(f"mc_create {collection}: no document id returned")
        return doc_id

    async def mc_get(self, collection: str, doc_id: str) -> dict | None:
        return await self._get(COLL[collection], doc_id)

    async def mc_replace(self, collection: str, doc_id: str, data: dict) -> None:
        await self._replace(COLL[collection], doc_id, data)

    async def mc_list(self, collection: str, queries: list[str] | None = None,
                      limit: int = 100) -> list[dict]:
        """List docs in an mc-link collection (random ids, query-driven)."""
        return await self._list(COLL[collection], queries, limit=limit)

    async def mc_get_auth(self, username: str) -> dict | None:
        """The mc_auth profile for an exact/case-sensitive username."""
        return await self._get(COLL["mc_auth"], username)

    async def mc_save_auth(self, username: str, data: dict) -> None:
        """Upsert an mc_auth profile (document id == the username)."""
        payload = dict(data)
        payload["username"] = username
        await self._replace(COLL["mc_auth"], username, payload)


store = Store()