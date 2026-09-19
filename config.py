"""Bot_CR configuration.

Every knob lives in the environment (loaded from a `.env` file next to this
module). Channel/role names that were previously hard-coded all over the cogs
now come from here, so the bot can be reconfigured without touching code.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _env(name: str, default=None, required: bool = False, cast=str):
    """Read an env var, cast it, and enforce required-ness."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


# ── Discord ────────────────────────────────────────────────────────────
BOT_TOKEN = _env("BOT_TOKEN", required=True)
PREFIX = _env("PREFIX", "!")

# Enables instant slash-command sync to one server (great for testing).
# Leave empty to sync commands globally (can take up to an hour to propagate).
GUILD_ID = _env("GUILD_ID", default=None, cast=int)

# ── Appwrite data store ────────────────────────────────────────────────
APPWRITE_ENDPOINT = _env("APPWRITE_ENDPOINT", "https://appwrite.alibks.dev/v1")
APPWRITE_PROJECT_ID = _env("APPWRITE_PROJECT_ID", "robotics-ops", required=True)
APPWRITE_API_KEY = _env("APPWRITE_API_KEY", required=True)
APPWRITE_DATABASE_ID = _env("APPWRITE_DATABASE_ID", "robotics_ops")

# ── Channel names (matches #channel names on the server) ───────────────
CHANNEL_RULES = _env("CHANNEL_RULES", "•📚•-rules-of-the-server")
CHANNEL_WELCOME = _env("CHANNEL_WELCOME", "•👋•-joins")
CHANNEL_GOODBYE = _env("CHANNEL_GOODBYE", "•👋•-leaves")
CHANNEL_VERIFICATION = _env("CHANNEL_VERIFICATION", "•📑•-verification")
CHANNEL_ANNOUNCEMENTS = _env("CHANNEL_ANNOUNCEMENTS", "⦿announcements⦿")
CHANNEL_DASHBOARD = _env("CHANNEL_DASHBOARD", "⦿dashboard⦿")
CHANNEL_BOTLOG = _env("CHANNEL_BOTLOG", "🤖bot-development")
CHANNEL_MAIN = _env("CHANNEL_MAIN", "「💬」main-chat")

# ── Role names ─────────────────────────────────────────────────────────
ROLE_TEMP = _env("ROLE_TEMP", "⛔ | None")
ROLE_VERIFIED = _env("ROLE_VERIFIED", "「📗」Verified")
ROLE_MEMBER = _env("ROLE_MEMBER", "🌿 | LVL 01+")

# Bot staff roles — holders get full access to every staff command,
# bypassing guild-permission checks (same as the server owner roll).
ROLE_ARCHON = _env("ROLE_ARCHON", "Archon")
ROLE_BOT_DEVELOPER = _env("ROLE_BOT_DEVELOPER", "Bot Developer")
ROLE_BOT_ADMIN = _env("ROLE_BOT_ADMIN", "Bot Admin")

# Extra Discord user IDs (comma-separated) that are always treated as bot
# staff, regardless of guild roles — e.g. the club's bot operator. Survives
# role reshuffles. Example: BOT_ADMIN_USER_IDS=407922956757499905
BOT_ADMIN_USER_IDS = {
    int(i) for i in _env("BOT_ADMIN_USER_IDS", "").split(",")
    if i.strip().isdigit()
}

# Who may control the Minecraft server (/mcstart, /mcstop, /mcrestart) — only
# these IDs plus the Archon role; deliberately NOT general bot staff, pres/VP
# or server admins. Defaults to the bot operator IDs so it works out of the
# box; set MC_CONTROL_USER_IDS to tighten or widen the list.
MC_CONTROL_USER_IDS = {
    int(i) for i in _env("MC_CONTROL_USER_IDS", _env("BOT_ADMIN_USER_IDS", ""))
    .split(",")
    if i.strip().isdigit()
}

# Leadership roles — /setlead may replace the holders of these.
ROLE_PRESIDENT = _env("ROLE_PRESIDENT", "President")
ROLE_VICE_PRESIDENT = _env("ROLE_VICE_PRESIDENT", "Vice President")
ROLE_LEAD = _env("ROLE_LEAD", "Lead")
# Comma-separated allowlist of leader role names for /setlead.
LEADER_ROLES = [s.strip() for s in _env(
    "LEADER_ROLES", "Lead,Vice President,President").split(",") if s.strip()]

# ── Behaviour ──────────────────────────────────────────────────────────
DASHBOARD_REFRESH_SECONDS = _env("DASHBOARD_REFRESH_SECONDS", 60, cast=int)
XP_COOLDOWN_SECONDS = _env("XP_COOLDOWN_SECONDS", 60, cast=int)
XP_MIN = _env("XP_MIN", 4, cast=int)
XP_MAX = _env("XP_MAX", 12, cast=int)

# ── Club website ───────────────────────────────────────────────────────
WEBSITE_URL = _env("WEBSITE_URL", "https://robotics.ma")

# ── Minecraft server (Pterodactyl Client API + status ping) ────────────
MC_PTERO_URL = _env("MC_PTERO_URL", "https://panel.minecraft.bouyakhsass.com").rstrip("/")
MC_PTERO_CLIENT_KEY = _env("MC_PTERO_CLIENT_KEY", "")
MC_SERVER_ID = _env("MC_SERVER_ID", "")
MC_ADDRESS = _env("MC_ADDRESS", "")
MC_PORT = _env("MC_PORT", 25566, cast=int)
MC_SERVER_NAME = _env("MC_SERVER_NAME", "Robotics CMC")

# Auto-assigned to every member who links a Minecraft account, so a single
# role mention pings the whole linked player base (used by /mcsession and the
# auto "someone joined" rally).
MC_PLAYER_ROLE = _env("MC_PLAYER_ROLE", "⛏️ Minecraft Player")

# How often the auto join-rally may ping the Minecraft player role (seconds).
MC_RALLY_COOLDOWN = _env("MC_RALLY_COOLDOWN", 2700, cast=int)

# ── mc-link (Discord ↔ Minecraft single sign-on) ───────────────────────
# AES-256-GCM key (32 bytes as hex) shared between this bot and the Paper
# plugin — used to encrypt one-time codes and temporary passwords written to
# mc_challenges.payload_enc. AAD bound to the plaintext username.
MC_LINK_SECRET = _env("MC_LINK_SECRET", "")
# Link codes expire after this many seconds (default 5 min).
MC_LINK_CODE_TTL = _env("MC_LINK_CODE_TTL", 300, cast=int)
# Temporary login passwords for new-IP challenges (default 5 min).
MC_TEMP_TTL = _env("MC_TEMP_TTL", 300, cast=int)
# Watcher poll interval for mc_link_codes / mc_challenges (seconds).
MC_LINK_POLL_SECONDS = _env("MC_LINK_POLL_SECONDS", 5, cast=int)