"""CipherBox and secret helpers for the mc-link handshake (Discord side).

Byte-for-byte twin of the Paper plugin's ``CipherBox`` so the two sides can
interop over ``mc_challenges.payload_enc``:

- AES-256-GCM, key = ``bytes.fromhex(config.MC_LINK_SECRET)`` (32 bytes).
- Random 12-byte IV per seal.
- AAD is bound to the **plaintext username** of the record.
- Payload layout: ``iv(12) + tag(16) + ciphertext``, then hex-encoded.

Unlike the plugin, this module also owns the *minting side* helpers (link
codes, temporary passwords) — the bot is the only component that mints
credentials; the plugin only consumes them.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 6-char link codes: unambiguous alphanumerics only (no 0/O, 1/I/L, 5/S, 8/B).
CODE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY234679"

# Temporary passwords: alphanumerics (same unambiguous alphabet) + -_.
TEMP_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_"


def iso_now() -> str:
    """ISO-8601 UTC timestamp for Appwrite datetime attributes / markers."""
    return datetime.now(timezone.utc).isoformat()


def dt_friendly() -> str:
    """Human-readable UTC stamp for the legacy bot_members.links display."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (Appwrite ``$updatedAt`` or our markers).

    Appwrite may emit ``…Z`` while our watcher markers use ``+00:00``; both are
    normalized so lexicographic-monotonic comparisons are safe. Returns None on
    any unparseable/empty input (never raises).
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def new_link_code() -> str:
    """Cryptographically random 6-char link code from the unambiguous alphabet."""
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def new_temp_password() -> str:
    """Cryptographically random 12-char one-time login password (temp only)."""
    return "".join(secrets.choice(TEMP_ALPHABET) for _ in range(12))


class CipherBox:
    """AES-256-GCM credential seal matching the server plugin's CipherBox."""

    def __init__(self, key_hex: str):
        key = bytes.fromhex(key_hex)
        if len(key) != 32:
            raise ValueError("MC_LINK_SECRET must be 32 bytes encoded as hex")
        self._cipher = AESGCM(key)

    def seal(self, username: str, plaintext: str) -> str:
        """Encrypt and sign secrets for a record bound to ``username``."""
        if not username:
            raise ValueError("AAD (username) must not be empty")
        iv = secrets.token_bytes(12)
        sealed = self._cipher.encrypt(iv, plaintext.encode("utf-8"),
                                      username.encode("utf-8"))
        # sealed = ciphertext || tag  →  payload = iv + tag + ciphertext.
        tag, ct = sealed[-16:], sealed[:-16]
        return (iv + tag + ct).hex()

    def open(self, username: str, token: str) -> str:
        """Decrypt and verify a sealed payload; raises on any tamper/mismatch."""
        raw = bytes.fromhex(token)
        if len(raw) < 28:
            raise ValueError("ciphertext too short")
        iv, tag, ct = raw[:12], raw[12:28], raw[28:]
        return self._cipher.decrypt(iv, ct + tag,
                                    username.encode("utf-8")).decode("utf-8")


def load_ips(raw: str | None) -> list[dict]:
    """Parse ``mc_auth.last_ips`` (JSON array of {ip, seen_at}) defensively."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def save_ips(rows: list[dict]) -> str:
    return json.dumps(rows)


def mask_ip(ip: str) -> str:
    """Harden screencaps: replace the final octet with *** by default."""
    parts = ip.strip().split(".")
    if len(parts) != 4:
        return ip
    return f"{'.'.join(parts[:3])}.***"