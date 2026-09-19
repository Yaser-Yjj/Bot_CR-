"""Club permission scopes for Bot_CR (not a cog).

Authorization pipeline: Discord interaction -> author -> linked club account
(record) -> club role -> scopes. Discord-held staff roles and the Archon role
bootstrap trust, but the linked club account is the source of truth for
hierarchy. Every club command is gated with :func:`require_scope`, so access
is declared per-command instead of scattered ``if role == ...`` checks.
"""

import discord
from discord.ext import commands

import config
from data.store import store
from data.store import StoreError
from cogs._perms import is_bot_admin

# ── Scope vocabulary ────────────────────────────────────────────────────
# Base: every member gets these.
BASE_SCOPES = {
    "profile.read",
    "members.read_public",
    "tasks.read",
    "competitions.read",
    "events.read",
    "fun",
}

ALL_SCOPES = BASE_SCOPES | {
    "tasks.claim",
    "tasks.create",
    "tasks.assign",
    "tasks.edit",
    "tasks.cancel",
    "events.create",
    "events.rsvp",
    "members.read",
    "members.manage",
    "cells.manage",
    "cells.assign",
    "cell.stats",
    "competitions.manage",
    "announcements.create",
    "internal.read",
    "admin.read",
    "admin.manage",
}

# Club-role keys that exist as hierarchy levels (low -> high).
HIERARCHY = [
    "core_member",
    "cell_member",
    "cell_chief",
    "vice_president",
    "president",
    "archon",
]

CLUB_ROLE_LABELS = {
    "core_member": "Core Member",
    "cell_member": "Cell Member",
    "cell_chief": "Cell Chief",
    "vice_president": "Vice President",
    "president": "President",
    "archon": "Archon",
}

# Extra scope sets granted at each level (base scopes are implicit).
ROLE_SCOPES = {
    "core_member": set(),
    "cell_member": {"tasks.claim", "events.rsvp"},
    "cell_chief": {
        "tasks.claim", "tasks.create", "tasks.assign", "tasks.edit",
        "tasks.cancel", "events.create", "events.rsvp",
        "members.read", "cells.assign", "cell.stats",
    },
    "vice_president": {
        "tasks.claim", "tasks.create", "tasks.assign", "tasks.edit",
        "tasks.cancel", "events.create", "events.rsvp",
        "members.read", "members.manage", "cells.manage", "cells.assign",
        "cell.stats",
        "competitions.manage", "announcements.create", "internal.read",
    },
    "president": {
        "tasks.claim", "tasks.create", "tasks.assign", "tasks.edit",
        "tasks.cancel", "events.create", "events.rsvp",
        "members.read", "members.manage", "cells.manage", "cells.assign",
        "cell.stats",
        "competitions.manage", "announcements.create", "internal.read",
        "admin.read",
    },
    "archon": ALL_SCOPES,
}

# Human-readable explanations, used by /roles.
SCOPE_LABELS = {
    "profile.read": "See your own club profile",
    "members.read_public": "See public member info",
    "members.read": "See internal member info",
    "members.manage": "Manage members (roles, cells)",
    "tasks.read": "Read tasks you can see",
    "tasks.claim": "Claim unassigned tasks",
    "tasks.create": "Create tasks",
    "tasks.assign": "Assign / edit / cancel tasks",
    "events.read": "View club events",
    "events.create": "Create events",
    "events.rsvp": "RSVP to events",
    "competitions.read": "View competitions",
    "competitions.manage": "Create / manage competitions",
    "cells.manage": "Manage cells across the club",
    "cells.assign": "Add members to a cell",
    "cell.stats": "See cell statistics",
    "announcements.create": "Create announcements",
    "internal.read": "Read internal club data",
    "admin.read": "Read governance data",
    "admin.manage": "Full administrative control",
    "fun": "Fun commands",
}


def _level_index(keys) -> int:
    return max(HIERARCHY.index(k) for k in keys if k in HIERARCHY)


def scopes_for(club_key: str | None, *, is_member: discord.Member = None) -> set:
    """Resolve scopes for a linked club role key.

    ``is_member`` (optional) adds the Discord-side trust boost: bot staff and
    the Archon/President/Vice-President/Lead Discord roles raise access even
    before the club account is linked.
    """
    if is_member is not None and is_bot_admin(is_member):
        return set(ALL_SCOPES)
    keys = set()
    if is_member is not None:
        names = {role.name for role in getattr(is_member, "roles", ())}
        if config.ROLE_ARCHON in names:
            keys.add("archon")
        if config.ROLE_PRESIDENT in names:
            keys.add("president")
        if config.ROLE_VICE_PRESIDENT in names:
            keys.add("vice_president")
        if config.ROLE_LEAD in names:
            keys.add("cell_chief")
    if club_key and club_key in HIERARCHY:
        keys.add(club_key)
    if not keys:
        return set(BASE_SCOPES)
    level = HIERARCHY[_level_index(keys)]
    if level == "archon":
        return set(ALL_SCOPES)
    return set(BASE_SCOPES) | set(ROLE_SCOPES.get(level, set()))


async def scopes_for_author(ctx) -> set:
    """Scopes for the invoking member (reads their linked club account)."""
    record = {}
    try:
        record = (await store.get_member(ctx.author.id)) or {}
    except StoreError:
        pass
    return scopes_for(str(record.get("club_role") or "").lower(),
                      is_member=ctx.author)


def require_scope(scope: str):
    """Gate a club command on a permission scope.

    Bot staff / server admins always pass; everyone else needs the scope,
    resolved from their Discord roles and linked club account.
    """
    async def predicate(ctx):
        if is_bot_admin(ctx.author):
            return True
        scopes = await scopes_for_author(ctx)
        if scope in scopes:
            return True
        raise commands.MissingPermissions([scope])
    return commands.check(predicate)