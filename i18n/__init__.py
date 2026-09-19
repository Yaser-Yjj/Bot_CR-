"""Lightweight localization for Bot_CR.

Interactive responses follow the invoking member's language:

    member.lang (stored) -> their Discord locale (if supported) -> English

Server-side broadcasts (welcome channel, dashboard tracker, announcements)
stay English by design — set by the server admin, not per user.

Languages: English (en), French (fr), Arabic (ar).
"""