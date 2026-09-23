"""Tiny i18n runtime: JSON string tables + language resolution.

No framework, no deps: one JSON file per language loaded at import, a ``t()``
lookup with ``{placeholder}`` formatting, and a resolver that walks
member-setting -> Discord locale -> English.

Importing this module pulls in ``data.store`` (used by ``resolve_member_lang``),
so keep it out of server-broadcast paths — those just call ``t("...", "en")``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

LOG = logging.getLogger("bot.i18n")

# Supported language codes -> endonym (shown as-is on language buttons).
LANGUAGES: dict[str, str] = {"en": "English", "fr": "Français", "ar": "العربية"}
_SUPPORTED = set(LANGUAGES)

_LANGUAGE_FLAGS = {"en": "🇬🇧", "fr": "🇫🇷", "ar": "🇸🇦"}

_DIR = Path(__file__).resolve().parent
_TABLES: dict[str, dict] = {}


def _load() -> None:
    for code in _SUPPORTED:
        path = _DIR / f"{code}.json"
        try:
            _TABLES[code] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.error("Could not load i18n table %s: %s", path, exc)
            _TABLES[code] = {}


_load()


def t(key: str, lang: str = "en", **kwargs) -> str:
    """Translate ``key`` into ``lang``, falling back to English then the key.

    ``{placeholders}`` in the string are filled from ``kwargs`` when given.
    """
    table = _TABLES.get(lang) or _TABLES.get("en") or {}
    text = table.get(key)
    if text is None:
        text = (_TABLES.get("en", {}) or {}).get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


def is_supported(lang: str | None) -> bool:
    return lang in _SUPPORTED


def locale_to_lang(locale: str | None) -> str:
    """Map a Discord locale code (e.g. ``fr``, ``ar-SA``, ``en-US``) to a
    supported language; anything unknown falls back to English."""
    if not locale:
        return "en"
    base = locale.split("-")[0].lower()
    return base if base in _SUPPORTED else "en"


def lang_label(lang: str) -> str:
    """Endonym + flag of a language code (e.g. ``🇫🇷 Français``)."""
    flag = _LANGUAGE_FLAGS.get(lang, "")
    return f"{flag} {LANGUAGES.get(lang, lang)}".strip()


async def resolve_member_lang(member_id: int, locale: str | None = None) -> str:
    """The language a member's interactive responses should use.

    Order: stored member.lang -> their Discord locale (if supported) -> en.
    Failures (Appwrite down, bad stored value) degrade gracefully to locale/en.
    """
    code: str | None = locale_to_lang(locale)
    try:
        from data.store import store

        doc = await store.get_member(member_id)
    except Exception as exc:  # noqa: BLE001 - persistence must never block replies
        LOG.warning("resolve_member_lang: store unavailable for %s: %s", member_id, exc)
        doc = None
    stored = (doc or {}).get("lang")
    if is_supported(stored):
        return stored
    return code