"""Staff-permission helpers for Bot_CR (not a cog).

Full bot-staff bypass: members holding the Archon / bot-developer / bot-admin
roles (plus server admins) can run every staff command, regardless of Discord
guild permissions. Regular members must still hold the required permission.
"""

import discord
from discord.ext import commands

import config


def is_bot_admin(member: discord.Member) -> bool:
    """True for server admins, whitelisted operator IDs and staff-role holders."""
    if member.id in config.BOT_ADMIN_USER_IDS:
        return True
    if member.guild_permissions.administrator:
        return True
    roles = {role.name for role in member.roles}
    staff = {config.ROLE_ARCHON, config.ROLE_BOT_DEVELOPER, config.ROLE_BOT_ADMIN}
    return bool(roles & staff)


def mod_perms(**perms):
    """Permission gate that bot-staff bypass.

    Combines ``has_permissions`` and ``bot_has_permissions`` into one check:
    the author (or the bot) must hold the requested guild permissions — unless
    the author is bot staff, in which case the command runs regardless.
    """

    async def predicate(ctx):
        if is_bot_admin(ctx.author):
            return True
        required = discord.Permissions(**perms)
        if not ctx.bot.user.guild_permissions.is_superset(required):
            raise commands.BotMissingPermissions(list(perms))
        if not ctx.author.guild_permissions.is_superset(required):
            raise commands.MissingPermissions(list(perms))
        return True

    return commands.check(predicate)