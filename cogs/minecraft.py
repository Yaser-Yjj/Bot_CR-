"""Minecraft bridge — Robotics CMC status + self-service whitelist.

Talks to the club's Pterodactyl panel through its Client API (start/stop
state, console command, file read/write of ``whitelist.json``) and does a
standard Minecraft server-list ping on the game port for the version and
player counts — the same protocol the multiplayer menu uses. **No Minecraft
plugins needed**: whitelisting is vanilla (whitelist.json is written directly
and live-reloaded via ``whitelist reload`` when the server is running), and
the ping works on any vanilla/paper server with nothing enabled server-side.

The server runs cracked/offline mode (``online-mode=false``), so matching is
by the UUID the client presents. ``/linkmc`` therefore asks the member to
declare their account type: **paid** accounts get their real UUID *and* the
offline UUID (so the official and free launchers both work), while **free**
accounts get only the offline UUID of the exact-case name they type
(``MD5("OfflinePlayer:<name>")`` — Mojang's capitalisation would be a
different account, e.g. ``hatim`` vs ``Hatim``).
"""

import asyncio
import hashlib
import json
import logging
import re
import struct
import time
import uuid as uuidlib
from datetime import datetime, timezone
from typing import Literal

import aiohttp
import discord
from discord.ext import commands

import config
from data.store import store
from cogs._scopes import scopes_for_author
from i18n.core import resolve_member_lang, t

LOG = logging.getLogger("bot.minecraft")

# Vanilla username rules: 1–16 chars of ASCII letters/digits/underscore.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_PING_TIMEOUT = 10.0


# ── Offline-mode UUID derivation ───────────────────────────────────────
def _offline_uuid(name: str) -> str:
    """The UUID a cracked/offline-mode server derives from a username.

    Same algorithm as vanilla: UUID v3 (MD5) of ``OfflinePlayer:<name>``.
    This is what must sit in whitelist.json for a cracked account to match,
    and it is what a cracked launcher sends on login.
    """
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30  # version 3
    digest[8] = (digest[8] & 0x3F) | 0x80  # RFC 4122 variant
    return str(uuidlib.UUID(bytes=bytes(digest)))


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for identity-link audit records."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── Pterodactyl Client API ─────────────────────────────────────────────
def _headers() -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {config.MC_PTERO_CLIENT_KEY}",
    }


def _server_url(*parts: str) -> str:
    base = f"{config.MC_PTERO_URL}/api/client/servers/{config.MC_SERVER_ID}"
    return "/".join([base, *parts])


async def server_state(session: aiohttp.ClientSession) -> str:
    """current_state: running / starting / stopping / offline."""
    async with session.get(_server_url("resources")) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return (data.get("attributes") or {}).get("current_state", "unknown")


async def server_name(session: aiohttp.ClientSession) -> str:
    async with session.get(_server_url()) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return (data.get("attributes") or {}).get("name", "")


async def read_whitelist(session: aiohttp.ClientSession) -> list:
    """Current whitelist.json entries ([] if missing/empty)."""
    async with session.get(_server_url("files", "contents"),
                           params={"file": "/whitelist.json"}) as resp:
        if resp.status == 404:
            return []
        resp.raise_for_status()
        text = await resp.text()
    try:
        entries = json.loads(text or "[]")
    except ValueError:
        return []
    return entries if isinstance(entries, list) else []


async def write_whitelist(session: aiohttp.ClientSession, entries: list) -> None:
    """Overwrite whitelist.json (used while the server is stopped)."""
    async with session.post(
        _server_url("files", "write"),
        params={"file": "/whitelist.json"},
        data=json.dumps(entries),
        headers={"Content-Type": "text/plain"},
    ) as resp:
        resp.raise_for_status()


async def send_command(session: aiohttp.ClientSession, command: str) -> None:
    """Pipe a console command to the running server (e.g. ``whitelist add``)."""
    async with session.post(_server_url("command"), json={"command": command}) as resp:
        resp.raise_for_status()


async def power_action(session: aiohttp.ClientSession, signal: str) -> None:
    """Send a Pterodactyl power signal: start, stop or restart (graceful)."""
    async with session.post(_server_url("power"), json={"signal": signal}) as resp:
        resp.raise_for_status()


async def mojang_profile(username: str) -> dict | None:
    """Resolve a Minecraft username to {id (dashed uuid), name}; None if unknown."""
    url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 204:
                    return None
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception:  # noqa: BLE001 - network hiccups mean "unknown", not failure
        return None


# ── Minecraft server-list ping (protocol >= 1.7) ───────────────────────
def _pack_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF  # -1 ("any protocol") must encode as an unsigned varint
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


async def _read_varint(reader: asyncio.StreamReader, timeout: float) -> int:
    result = 0
    shift = 0
    while True:
        b = (await asyncio.wait_for(reader.readexactly(1), timeout))[0]
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result
        shift += 7


def _parse_varint(buf: bytes, offset: int):
    result = 0
    shift = 0
    while True:
        b = buf[offset]
        result |= (b & 0x7F) << shift
        offset += 1
        if not (b & 0x80):
            return result, offset
        shift += 7


async def status_ping(host: str, port: int,
                      timeout: float = _PING_TIMEOUT) -> dict:
    """Server-list ping -> {version, players{online,max}, description, ...}."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout)
    try:
        host_b = host.encode()
        payload = (b"\x00" + _pack_varint(-1) + _pack_varint(len(host_b))
                   + host_b + struct.pack(">H", port) + _pack_varint(1))
        writer.write(_pack_varint(len(payload)) + payload)
        writer.write(_pack_varint(1) + b"\x00")  # status request
        await writer.drain()
        packet_len = await _read_varint(reader, timeout)
        body = await asyncio.wait_for(reader.readexactly(packet_len), timeout)
        _pid, off = _parse_varint(body, 0)          # packet id (0x00)
        json_len, off = _parse_varint(body, off)    # JSON string length
        text = body[off:off + json_len].decode("utf-8", "replace")
        return json.loads(text)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001 - best-effort socket close
            pass


# ── Cog ────────────────────────────────────────────────────────────────
class Minecraft(commands.Cog):
    """Robotics CMC server status, self-service whitelist and player tagging."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Console websocket (join/leave detection) state.
        self._ws_stop = asyncio.Event()
        self._ws_task: asyncio.Task | None = None
        self._online: dict[str, float] = {}     # mc username -> joined timestamp
        self._pending_rally: set[str] = set()   # burst coalescing window
        self._rally_task: asyncio.Task | None = None
        self._rally_cooldown_until = 0.0
        self._backfilled_roles = False

    async def cog_load(self) -> None:
        """Start the Pterodactyl console watcher (plugin-free join/leave feed)."""
        if self._configured():
            self._ws_task = asyncio.create_task(self._console_watch())

    def cog_unload(self) -> None:
        """Stop the watcher and any pending rally."""
        self._ws_stop.set()
        if self._rally_task is not None:
            self._rally_task.cancel()
        if self._ws_task is not None:
            self._ws_task.cancel()

    def _configured(self) -> bool:
        return bool(config.MC_PTERO_CLIENT_KEY and config.MC_SERVER_ID
                    and config.MC_ADDRESS)

    @staticmethod
    def _is_mc_operator(member: discord.Member) -> bool:
        """MC power control is restricted to the operator and the Archon role.

        Deliberately much narrower than :func:`cogs._perms.is_bot_admin`:
        pres/VP, other staff roles and members with server-administrator
        permissions are all excluded — only the operator's user ID and the
        Archon role may start/stop/restart the server.
        """
        if member.id in config.MC_CONTROL_USER_IDS:
            return True
        wanted = config.ROLE_ARCHON.strip().lower()
        return any(
            role.name.strip().lower() == wanted or "archon" in role.name.strip().lower()
            for role in member.roles
        )

    @staticmethod
    def _can_manage_link(user: discord.Member, member: discord.Member) -> bool:
        """Only the profile owner (or an MC operator) may link/unlink it.

        The Minecraft hub always passes the same member, so this is trivially
        true there; it only bites on someone else's /profile.
        """
        return user.id == member.id or Minecraft._is_mc_operator(user)

    async def _require_configured(self, ctx, lang: str) -> bool:
        if self._configured():
            return True
        await ctx.send(t("mc.unconfigured", lang))
        return False

    async def _lang(self, ctx) -> str:
        locale = str(ctx.interaction.locale) if ctx.interaction else None
        return await resolve_member_lang(ctx.author.id, locale)

    @staticmethod
    def _new_session() -> aiohttp.ClientSession:
        return aiohttp.ClientSession(headers=_headers(), timeout=_TIMEOUT)

    # ── player tagging (role + auto join-rally) ───────────────
    async def _mc_player_role(self, guild: discord.Guild) -> discord.Role | None:
        """Find (or create) the 'Minecraft Player' role used for tagging."""
        role = discord.utils.get(guild.roles, name=config.MC_PLAYER_ROLE)
        if role is None:
            try:
                role = await guild.create_role(
                    name=config.MC_PLAYER_ROLE,
                    reason="Minecraft player tagging role",
                )
            except discord.HTTPException as exc:
                LOG.warning("Could not create MC player role in %s: %s",
                            guild.id, exc)
                return None
        return role

    async def _ensure_mc_role(self, member: discord.Member) -> None:
        """Best-effort role assignment on link — never fails the flow."""
        if getattr(member, "guild", None) is None:
            return
        try:
            role = await self._mc_player_role(member.guild)
            if role is not None and role not in member.roles:
                await member.add_roles(role, reason="Linked Minecraft account")
        except Exception as exc:  # noqa: BLE001 - tagging is best-effort
            LOG.warning("Could not assign MC player role to %s: %s",
                        member.id, exc)

    async def _drop_mc_role(self, member: discord.Member) -> None:
        """Best-effort role removal on unlink."""
        if getattr(member, "guild", None) is None:
            return
        try:
            role = discord.utils.get(member.guild.roles,
                                     name=config.MC_PLAYER_ROLE)
            if role is not None and role in member.roles:
                await member.remove_roles(role, reason="Unlinked Minecraft account")
        except Exception as exc:  # noqa: BLE001 - tagging is best-effort
            LOG.warning("Could not drop MC player role from %s: %s",
                        member.id, exc)

    @commands.Cog.listener()
    async def on_ready(self):
        """One-shot role backfill: every linked player must wear the tag."""
        if self._backfilled_roles:
            return
        self._backfilled_roles = True
        self.bot.loop.create_task(self._backfill_mc_roles())

    async def _backfill_mc_roles(self) -> None:
        for guild in self.bot.guilds:
            try:
                role = await self._mc_player_role(guild)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("MC role backfill skipped %s: %s", guild.id, exc)
                continue
            if role is None:
                continue
            fixed = 0
            for member in guild.members:
                if member.bot:
                    continue
                record = await self._record_for(member.id)
                if self._mc_link_of(record) and role not in member.roles:
                    try:
                        await member.add_roles(role,
                                               reason="MC role backfill (linked)")
                        fixed += 1
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("MC backfill role on %s failed: %s",
                                    member.id, exc)
            LOG.info("MC player role backfill on %s: %s roles added", guild.id, fixed)

    async def _console_endpoint(self, session: aiohttp.ClientSession
                                ) -> tuple[str, str]:
        """Fresh websocket (token, socket-url) for the console stream."""
        async with session.get(_server_url("websocket")) as resp:
            resp.raise_for_status()
            data = (await resp.json())["data"]
        return data["token"], data["socket"]

    async def _console_watch(self) -> None:
        """Stream the Pterodactyl console for join/leave lines (plugin-free)."""
        backoff = 5.0
        while not self._ws_stop.is_set():
            try:
                async with self._new_session() as session:
                    token, url = await self._console_endpoint(session)
                    async with session.ws_connect(
                            url, headers={"Origin": config.MC_PTERO_URL},
                            heartbeat=30.0) as ws:
                        await ws.send_json({"event": "auth", "args": [token]})
                        backoff = 5.0
                        async for msg in ws:
                            if self._ws_stop.is_set():
                                return
                            if msg.type != aiohttp.WSMsgType.TEXT:
                                if msg.type in (aiohttp.WSMsgType.CLOSED,
                                                aiohttp.WSMsgType.ERROR):
                                    break
                                continue
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue
                            evt = data.get("event")
                            args = data.get("args") or []
                            if evt == "console output":
                                line = " ".join(str(a) for a in args)
                                self._consume_console_line(line)
                            elif evt == "status":
                                state = str(args[0]) if args else ""
                                if state != "running":
                                    self._online.clear()
                            elif evt == "token expiring" and args:
                                # Panel hands a fresh token; re-auth with it.
                                await ws.send_json(
                                    {"event": "auth", "args": [str(args[0])]})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any drop
                LOG.warning("MC console watch dropped: %s", exc)
            if self._ws_stop.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    _JOIN_RE = re.compile(r"\]:\s*([A-Za-z0-9_]{1,16}) joined the game")
    _LEAVE_RE = re.compile(
        r"\]:\s*([A-Za-z0-9_]{1,16}) (?:left the game|lost connection:.*|disconnected)")

    def _consume_console_line(self, line: str) -> None:
        """Track online players from console output; schedule the rally ping."""
        m = self._JOIN_RE.search(line)
        if m:
            name = m.group(1)
            if name in self._online:
                return
            self._online[name] = time.time()
            self._pending_rally.add(name)
            self._schedule_rally()
            return
        m = self._LEAVE_RE.search(line)
        if m:
            self._online.pop(m.group(1), None)

    def _schedule_rally(self) -> None:
        if self._rally_task is not None and not self._rally_task.done():
            return
        self._rally_task = asyncio.create_task(self._flush_rally())

    async def _flush_rally(self) -> None:
        """Coalesce bursts, then one mention-limited role ping per cooldown."""
        await asyncio.sleep(10.0)  # let a squad's joins land in one message
        names = list(self._pending_rally)
        self._pending_rally.clear()
        if not names:
            return
        if time.time() < self._rally_cooldown_until:
            return
        self._rally_cooldown_until = (time.time()
                                      + config.MC_RALLY_COOLDOWN)
        try:
            for guild in self.bot.guilds:
                channel = discord.utils.get(guild.text_channels,
                                            name=config.CHANNEL_ANNOUNCEMENTS)
                if channel is None:
                    continue
                role = await self._mc_player_role(guild)
                mention = role.mention if role else "🎮"
                shown = names[:5]
                extra = f" and {len(names) - 5} more" if len(names) > 5 else ""
                text = (f"{mention} **{', '.join(shown)}{extra}** just hopped on "
                        f"**{config.MC_SERVER_NAME}** — jump in! "
                        f"`{config.MC_ADDRESS}:{config.MC_PORT}`")
                await channel.send(text)
                break
        except Exception as exc:  # noqa: BLE001 - a rally must never crash a task
            LOG.warning("MC join rally failed: %s", exc)

    # ── status ────────────────────────────────────────────────
    @commands.hybrid_command(name="mc",
                             description="Robotics CMC menu: status, link, unlink, server control.")
    @commands.guild_only()
    async def mc(self, ctx: commands.Context):
        await self._menu(ctx)

    @commands.hybrid_command(name="minecraft",
                             description="Robotics CMC menu: status, link, unlink, server control.")
    @commands.guild_only()
    async def minecraft(self, ctx: commands.Context):
        await self._menu(ctx)

    async def _menu(self, ctx: commands.Context):
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        lang = await self._lang(ctx)
        if not await self._require_configured(ctx, lang):
            return
        embed, view = await render_mc_hub(self, lang, ctx.author)
        await ctx.send(embed=embed, view=view)

    async def _record_for(self, user_id: int) -> dict:
        try:
            return (await store.get_member(user_id)) or {}
        except Exception:  # noqa: BLE001 - store down means "no record", never crash
            return {}

    @staticmethod
    def _links_of(record: dict) -> dict:
        """The member's per-platform identity map, whatever its stored shape.

        Appwrite has no JSON attribute type, so ``links`` lives as a JSON
        string; legacy/foreign docs may already carry a real dict. Both are
        accepted here and a single canonical dict is returned.
        """
        raw = (record or {}).get("links")
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except (ValueError, TypeError):
                LOG.debug("Ignoring unparsable links on member %s",
                          (record or {}).get("user_id"))
        return {}

    @staticmethod
    def _mc_link_of(record: dict) -> dict | None:
        """The member's structured links.minecraft entry, or None."""
        link = Minecraft._links_of(record).get("minecraft") or {}
        return link if link.get("username") else None

    async def _save_mc_link(self, user_id: int, canonical: str, account_type: str,
                            entries: list, linked_at: str) -> None:
        """Persist the Discord ↔ Minecraft link as the structured identity slot.

        ``links`` is read-modify-write so future platforms (e.g. ``robotics``)
        survive alongside ``minecraft``; ``mc_username`` stays for back-compat.
        The map is stored as JSON text (Appwrite has no object attribute type).
        """
        record = await self._record_for(user_id)
        links = Minecraft._links_of(record)
        links["minecraft"] = {
            "username": canonical,
            "type": account_type,
            "uuids": [e["uuid"] for e in entries],
            "linked_at": linked_at,
        }
        await store.merge_member(user_id, {"links": json.dumps(links),
                                           "mc_username": canonical})

    async def _clear_mc_link(self, user_id: int) -> None:
        record = await self._record_for(user_id)
        links = Minecraft._links_of(record)
        links.pop("minecraft", None)
        await store.merge_member(user_id, {"links": json.dumps(links),
                                           "mc_username": ""})

    async def _fetch_status(self, lang: str) -> tuple[dict, str | None]:
        """Live panel data {state, name, version, players} + error text (or None)."""
        version = None
        players = None
        state = "offline"
        name = config.MC_SERVER_NAME
        try:
            async with self._new_session() as session:
                state = await server_state(session)
                stored = await server_name(session)
                if stored:
                    name = stored
                if state in ("running", "starting"):
                    try:
                        ping_info = await status_ping(config.MC_ADDRESS,
                                                      config.MC_PORT)
                        version = (ping_info.get("version") or {}).get("name")
                        players = ping_info.get("players") or {}
                    except Exception as exc:  # ping is best-effort
                        LOG.warning("Minecraft status ping failed: %s", exc)
        except Exception as exc:
            LOG.warning("Minecraft status: Pterodactyl API failed: %s", exc)
            return {"state": "offline"}, t("mc.panel_unreachable", lang)
        return {"state": state, "name": name, "version": version,
                "players": players}, None

    async def _status_embed(self, lang: str, status: dict,
                            *, include_guide: bool = True) -> discord.Embed:
        state = status.get("state", "offline")
        name = status.get("name") or config.MC_SERVER_NAME
        version = status.get("version")
        players = status.get("players")
        state_emoji = {
            "running": t("mc.status.running", lang),
            "starting": t("mc.status.starting", lang),
            "stopping": t("mc.status.stopping", lang),
        }.get(state, t("mc.status.offline", lang))
        embed = discord.Embed(title=t("mc.title", lang, name=name), color=0x55AA55)
        embed.add_field(name=t("mc.field.ip", lang),
                        value=f"`{config.MC_ADDRESS}:{config.MC_PORT}`")
        embed.add_field(name=t("mc.field.version", lang), value=version or "—")
        embed.add_field(name=t("mc.field.status", lang), value=state_emoji)
        if players is not None:
            online = players.get("online", 0)
            value = f"{online}/{players.get('max', 0)}"
            if online and self._online:
                names = sorted(self._online)[:6]
                extra = " +…" if len(self._online) > len(names) else ""
                value += f" · {', '.join(names) + extra}"
            embed.add_field(name=t("mc.field.players", lang), value=value)
        else:
            embed.add_field(name=t("mc.field.players", lang), value="—")
        if include_guide:
            embed.add_field(name=t("mc.linking.title", lang),
                            value=t("mc.linking.body", lang))
        embed.set_footer(text=t("mc.footer", lang))
        return embed

    async def _hub_embed(self, lang: str, member: discord.Member) -> discord.Embed:
        status, error = await self._fetch_status(lang)
        embed = await self._status_embed(lang, status)
        if error:
            embed.add_field(name="⚠️", value=error, inline=False)
        link = self._mc_link_of(await self._record_for(member.id))
        if link:
            embed.add_field(
                name=t("mc.hub.your_link", lang),
                value=t("mc.hub.linked_value", lang, name=link["username"],
                        type=t(f"mc.link.type_{link.get('type', 'free')}", lang)),
                inline=False,
            )
        else:
            embed.add_field(
                name=t("mc.hub.your_link", lang),
                value=t("mc.hub.not_linked", lang) + "\n" + t("mc.hub.link_hint", lang),
                inline=False,
            )
        return embed

    # ── whitelist ─────────────────────────────────────────────
    @commands.hybrid_command(name="linkmc",
                             description="Whitelist a Minecraft username (free or paid account).")
    @commands.guild_only()
    @commands.cooldown(3, 60, commands.BucketType.user)
    async def linkmc(self, ctx: commands.Context, username: str,
                     account: Literal["free", "paid"]):
        """Add <username> to Robotics CMC's whitelist (self-service).

        account: \"paid\" if it's a bought Minecraft account (adds the real
        UUID so the official launcher works too), \"free\" for offline/cracked
        accounts (adds the offline UUID of the exact name as typed).
        """
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        lang = await self._lang(ctx)
        if not await self._require_configured(ctx, lang):
            return
        ok, text = await self._run_link(ctx.author, username, account, lang)
        await ctx.send(text)

    async def _run_link(self, member: discord.Member, username: str,
                        account: str, lang: str) -> tuple[bool, str]:
        """Prepare + apply a whitelist link (shared by /linkmc and the menu).

        Returns (ok, message). On success also persists the structured
        ``links.minecraft`` identity record for audit and the profile hub.
        """
        username = username.strip()
        if not _USERNAME_RE.match(username):
            return False, t("linkmc.not_username", lang)
        if account == "paid":
            # Paid account — whitelist the REAL uuid (official launcher) AND
            # the offline uuid of the same exact name (offline launchers), so
            # the member is covered whichever launcher they use.
            profile = await mojang_profile(username)
            if profile:
                canonical = profile.get("name", username)
                try:
                    real_uuid = str(uuidlib.UUID(profile["id"]))
                except (KeyError, TypeError, ValueError):
                    real_uuid = _offline_uuid(canonical)  # never write junk
                entries_to_add = [
                    {"uuid": real_uuid, "name": canonical},
                    {"uuid": _offline_uuid(canonical), "name": canonical},
                ]
                note = t("linkmc.note_paid", lang)
            else:
                # Claimed paid but Mojang doesn't know the name — don't write
                # a fake real UUID; add the offline entry and tell the member.
                canonical = username
                entries_to_add = [
                    {"uuid": _offline_uuid(canonical), "name": canonical},
                ]
                note = t("linkmc.note_paid_unknown", lang)
        else:
            # Free (cracked) account: whitelist the offline uuid of the EXACT
            # name as typed. Mojang's capitalisation of the same letters is a
            # different account ("hatim" !== "Hatim"), so it must never be
            # reused here.
            canonical = username
            entries_to_add = [
                {"uuid": _offline_uuid(canonical), "name": canonical},
            ]
            note = t("linkmc.note_free", lang)

        try:
            async with self._new_session() as session:
                state = await server_state(session)
                existing = await read_whitelist(session)
                known = {str(e.get("uuid")) for e in existing}
                fresh = [e for e in entries_to_add if e["uuid"] not in known]
                if not fresh:
                    return False, t("linkmc.already", lang, name=canonical)
                existing.extend(fresh)
                # Write the file directly and reload if running — uniform for
                # both states, and the only way to get the real-UUID entry in
                # (console \"whitelist add\" only derives the offline UUID on
                # an offline-mode server).
                await write_whitelist(session, existing)
                if state == "running":
                    await send_command(session, "whitelist reload")
                    msg = t("linkmc.success_running", lang, name=canonical)
                else:
                    msg = t("linkmc.success_stopped", lang, name=canonical)
        except Exception as exc:
            LOG.warning("linkmc failed for %r: %s", canonical, exc)
            return False, t("linkmc.failed", lang)
        try:
            await self._save_mc_link(member.id, canonical, account,
                                     entries_to_add, _now_iso())
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("linkmc: could not save link for %s: %s", member.id, exc)
        await self._ensure_mc_role(member)
        return True, msg + note + t("linkmc.linked_audit", lang)

    async def _run_unlink(self, member: discord.Member, lang: str) -> tuple[bool, str]:
        """Remove the member's whitelist entries + identity record (menu flow)."""
        link = self._mc_link_of(await self._record_for(member.id))
        if not link:
            return False, t("mc.unlink.not_linked", lang)
        name = link["username"]
        uuids = {str(u) for u in (link.get("uuids") or [])}
        try:
            async with self._new_session() as session:
                state = await server_state(session)
                existing = await read_whitelist(session)
                kept = [
                    e for e in existing
                    if str(e.get("uuid")) not in uuids
                    and str(e.get("name")) != name
                ]
                if len(kept) != len(existing):
                    await write_whitelist(session, kept)
                    if state == "running":
                        await send_command(session, "whitelist reload")
        except Exception as exc:
            LOG.warning("unlink failed for %s: %s", member.id, exc)
            return False, t("mc.unlink.failed", lang)
        try:
            await self._clear_mc_link(member.id)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("unlink: could not clear record for %s: %s", member.id, exc)
        await self._drop_mc_role(member)
        return True, t("mc.unlink.done", lang, name=name)

    @linkmc.error
    async def linkmc_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, (commands.MissingRequiredArgument,
                              commands.BadArgument)):
            lang = await self._lang(ctx)
            await ctx.send(
                t("linkmc.usage_title", lang) + "\n" + t("linkmc.usage_body", lang)
            )
            return
        raise error  # cooldown and friends keep the default handling

    # ── server power control (operator/Archon only) ──────────
    @commands.hybrid_command(name="mcstart",
                             description="Start the Minecraft server. (Operator or Archon only)")
    @commands.guild_only()
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def mcstart(self, ctx: commands.Context):
        """Boot the Robotics CMC server."""
        await self._power(ctx, "start")

    @commands.hybrid_command(name="mcstop",
                             description="Stop the Minecraft server. (Operator or Archon only)")
    @commands.guild_only()
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def mcstop(self, ctx: commands.Context):
        """Gracefully shut down the Robotics CMC server."""
        await self._power(ctx, "stop")

    @commands.hybrid_command(name="mcrestart",
                             description="Restart the Minecraft server. (Operator or Archon only)")
    @commands.guild_only()
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def mcrestart(self, ctx: commands.Context):
        """Gracefully restart the Robotics CMC server."""
        await self._power(ctx, "restart")

    async def _power_core(self, member: discord.Member, lang: str,
                          signal: str) -> tuple[bool, str]:
        """Shared power-signal core used by the slash commands and the menu.

        Returns (ok, message). Callers must already have checked
        ``_is_mc_operator`` and configuration; this handles state guards and
        the actual Pterodactyl power call.
        """
        try:
            async with self._new_session() as session:
                state = await server_state(session)
                if signal == "start" and state in ("running", "starting"):
                    return False, t("mc.ctrl.already_running", lang)
                if signal == "stop" and state in ("offline", "stopping"):
                    return False, t("mc.ctrl.already_offline", lang)
                if signal == "restart" and state == "offline":
                    return False, t("mc.ctrl.need_running", lang)
                await power_action(session, signal)
        except Exception as exc:
            LOG.warning("mc power %s failed: %s", signal, exc)
            return False, t("mc.ctrl.failed", lang)
        return True, t(f"mc.ctrl.{signal}_sent", lang)

    async def _power(self, ctx: commands.Context, signal: str):
        lang = await self._lang(ctx)
        if not self._is_mc_operator(ctx.author):
            await ctx.send(t("mc.ctrl.deny", lang))
            return
        if not await self._require_configured(ctx, lang):
            return
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        _ok, text = await self._power_core(ctx.author, lang, signal)
        await ctx.send(text)

    # ── /mcsession: ping the whole linked player base ─────────
    @commands.hybrid_command(
        name="mcsession",
        description="Ping every linked Minecraft player (MC operator or staff).")
    @commands.guild_only()
    async def mcsession(self, ctx: commands.Context, message: str = None):
        """Broadcast a join-call to all linked Minecraft players.

        Usable by MC operators (via the panel console key) and by anyone
        with ``announcements.create`` (VP+ / bot staff). The mention works
        because every linked player wears the auto-assigned Minecraft
        Player role. The announcement card itself is always English —
        server broadcasts are intentionally not localized.
        """
        lang = await self._lang(ctx)
        is_operator = self._is_mc_operator(ctx.author)
        if not is_operator:
            scopes = await scopes_for_author(ctx)
            if "announcements.create" not in scopes:
                await ctx.send(t("mcsession.deny", lang))
                return
        if not await self._require_configured(ctx, lang):
            return
        try:
            await ctx.defer()
        except discord.HTTPException:
            pass
        channel = (discord.utils.get(ctx.guild.text_channels,
                                     name=config.CHANNEL_ANNOUNCEMENTS)
                   or ctx.channel)
        role = await self._mc_player_role(ctx.guild)
        mention = role.mention if role else "🎮"
        text = (message or t("mcsession.default", lang)).strip()
        card = (f"{mention} 🎮 **{text}**\n\n"
                f"📡 **{config.MC_SERVER_NAME}** — "
                f"`{config.MC_ADDRESS}:{config.MC_PORT}`")
        try:
            await channel.send(card)
        except discord.HTTPException as exc:
            LOG.warning("mcsession broadcast failed: %s", exc)
            await ctx.send(t("mcsession.failed", lang))
            return
        await ctx.send(t("mcsession.sent", lang, channel=channel.mention))


# ── Interactive menu views (settings-style button drill-downs) ─────────
def _flow_embed(ok: bool, text: str) -> discord.Embed:
    """One-shot result card shown at the end of a link/unlink/control flow."""
    return discord.Embed(title="✅" if ok else "⚠️", description=text,
                         color=0x55AA55 if ok else 0xCC5555)


def _loading_embed() -> discord.Embed:
    return discord.Embed(description="⏳", color=discord.Color.greyple())


async def render_mc_hub(cog: "Minecraft", lang: str,
                        member: discord.Member
                        ) -> tuple[discord.Embed, "MinecraftHubView"]:
    """Fresh hub page: live status + the member's link summary + actions.

    The ``🔑 Change password`` / ``📱 Devices`` buttons only appear for
    members whose link came from mc-link (``links.minecraft.type == "linked"``
    — i.e. a real ``mc_auth`` profile exists to act on).
    """
    embed = await cog._hub_embed(lang, member)
    linked = None
    link = cog._mc_link_of(await cog._record_for(member.id))
    if link is not None and link.get("type") == "linked":
        linked = link.get("username")
    return embed, MinecraftHubView(cog, lang, member, linked_username=linked)


async def mc_link_card_embed(cog: "Minecraft", member: discord.Member,
                             lang: str, *, full: bool = True) -> discord.Embed:
    """'Your Minecraft link' status card, shared by /profile and the hub.

    ``full=False`` (viewing someone else's profile) hides the whitelisted
    UUIDs — username/type/date stay visible.
    """
    link = cog._mc_link_of(await cog._record_for(member.id))
    embed = discord.Embed(title=t("mc.hub.your_link_title", lang), color=0x55AA55)
    if link:
        embed.add_field(name=t("mc.link.status_username", lang),
                        value=f"`{link['username']}`", inline=True)
        embed.add_field(name=t("mc.link.status_type", lang),
                        value=t(f"mc.link.type_{link.get('type', 'free')}", lang),
                        inline=True)
        if full:
            uuids = "\n".join(f"`{u}`" for u in (link.get("uuids") or [])) or "—"
            embed.add_field(name=t("mc.link.status_uuids", lang), value=uuids,
                            inline=False)
        if link.get("linked_at"):
            embed.set_footer(text=t("mc.link.status_linked_at", lang,
                                    when=link["linked_at"]))
    else:
        embed.description = (t("mc.hub.not_linked", lang) + "\n\n"
                             + t("mc.hub.link_hint", lang))
    return embed


async def _back_to_home(view: discord.ui.View, interaction: discord.Interaction):
    """Route back up through the parent flow (or just dismiss the message).

    The parent (hub/profile tab) re-fetches live state, so we acknowledge with
    a spinner first and rewrite the message afterwards — never hold the button
    interaction open past Discord's 3 s response window.
    """
    if not getattr(view, "home_factory", None):
        await interaction.response.edit_message(embed=_loading_embed(), view=None)
        return
    await interaction.response.edit_message(embed=_loading_embed(), view=None)
    try:
        embed, target = await view.home_factory()
    except Exception:  # noqa: BLE001 - a broken parent must never hang a button
        await interaction.message.edit(
            embed=_flow_embed(False, t("mc.panel_unreachable",
                                      getattr(view, "lang", "en"))),
            view=None)
        return
    await interaction.message.edit(embed=embed, view=target)


class MinecraftHubView(discord.ui.View):
    """/mc + /minecraft menu: live status content with action drill-downs."""

    def __init__(self, cog: "Minecraft", lang: str, member: discord.Member,
                 *, linked_username: str | None = None,
                 timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.member = cog, lang, member
        self.linked_username = linked_username
        self.refresh.label = t("mc.hub.refresh", lang)
        self.link.label = t("mc.hub.link", lang)
        self.unlink.label = t("mc.hub.unlink", lang)
        self.control.label = t("mc.hub.control", lang)
        self.close.label = t("mc.hub.close", lang)
        self.mcpass.label = t("mc.hub.mcpass", lang)
        self.devices.label = t("mc.hub.devices", lang)
        if not cog._is_mc_operator(member):
            self.remove_item(self.control)
        # mc-link extras only make sense for a real mc_auth profile.
        if linked_username is None:
            self.remove_item(self.mcpass)
            self.remove_item(self.devices)

    async def _home(self) -> tuple[discord.Embed, "MinecraftHubView"]:
        return await render_mc_hub(self.cog, self.lang, self.member)

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.secondary, row=0)
    async def refresh(self, interaction: discord.Interaction,
                      _button: discord.ui.Button):
        await interaction.response.edit_message(embed=_loading_embed(), view=None)
        embed, view = await self._home()
        await interaction.message.edit(embed=embed, view=view)

    @discord.ui.button(emoji="🔗", style=discord.ButtonStyle.primary, row=0)
    async def link(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        view = LinkChoiceView(self.cog, self.lang, self.member,
                              home_factory=self._home)
        await interaction.response.edit_message(embed=await view.embed(), view=view)

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger, row=0)
    async def unlink(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        view = UnlinkConfirmView(self.cog, self.lang, self.member,
                                 home_factory=self._home)
        await interaction.response.edit_message(embed=await view.embed(), view=view)

    @discord.ui.button(emoji="🎛", style=discord.ButtonStyle.primary, row=1)
    async def control(self, interaction: discord.Interaction,
                      _button: discord.ui.Button):
        view = ControlView(self.cog, self.lang, self.member,
                           home_factory=self._home)
        await interaction.response.edit_message(embed=await view.embed(), view=view)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction,
                    _button: discord.ui.Button):
        await interaction.response.edit_message(
            content=t("mc.hub.closed", self.lang), embed=None, view=None)

    @discord.ui.button(emoji="🔑", style=discord.ButtonStyle.primary, row=1)
    async def mcpass(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        mclink = self.cog.bot.get_cog("McLink")
        if mclink is None or not getattr(self, "linked_username", None):
            await interaction.response.defer()
            return
        await mclink.open_mcpass_modal(interaction, self.lang,
                                       self.linked_username)

    @discord.ui.button(emoji="📱", style=discord.ButtonStyle.secondary, row=1)
    async def devices(self, interaction: discord.Interaction,
                      _button: discord.ui.Button):
        mclink = self.cog.bot.get_cog("McLink")
        if mclink is None or not getattr(self, "linked_username", None):
            await interaction.response.defer()
            return
        await mclink.open_devices_view(interaction, self.lang,
                                       self.linked_username,
                                       home_factory=self._home)


class LinkChoiceView(discord.ui.View):
    """Step 1 of the link flow: paid or free account (then a username modal)."""

    def __init__(self, cog: "Minecraft", lang: str, member: discord.Member,
                 *, home_factory=None, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.member = cog, lang, member
        self.home_factory = home_factory
        self.paid.label = t("mc.link.type_paid", lang)
        self.free.label = t("mc.link.type_free", lang)
        self.back.label = t("mc.hub.back", lang)

    async def embed(self) -> discord.Embed:
        return discord.Embed(
            title=t("mc.link.ask_title", self.lang),
            description=t("mc.link.ask_body", self.lang),
            color=discord.Color.blurple(),
        )

    @discord.ui.button(emoji="💳", style=discord.ButtonStyle.primary, row=0)
    async def paid(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        await self._pick(interaction, "paid")

    @discord.ui.button(emoji="🆓", style=discord.ButtonStyle.secondary, row=0)
    async def free(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        await self._pick(interaction, "free")

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        await _back_to_home(self, interaction)

    async def _pick(self, interaction: discord.Interaction, account_type: str):
        modal = LinkUsernameModal(self.cog, self.lang, self.member, account_type,
                                  home_factory=self.home_factory)
        await interaction.response.send_modal(modal)


class LinkUsernameModal(discord.ui.Modal):
    """Step 2 of the link flow: the exact Minecraft username to whitelist."""

    def __init__(self, cog: "Minecraft", lang: str, member: discord.Member,
                 account_type: str, *, home_factory=None):
        super().__init__(title=t("mc.link.modal_title", lang))
        self.cog, self.lang, self.member = cog, lang, member
        self.account_type = account_type
        self.home_factory = home_factory
        self.username = discord.ui.TextInput(
            label=t("mc.link.modal_username", lang),
            placeholder="Steve_08", required=True, max_length=64,
        )
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if not Minecraft._can_manage_link(interaction.user, self.member):
            view = ResultBackView(self.lang, self.home_factory)
            await interaction.edit_original_response(
                embed=_flow_embed(False, t("mc.link.deny_other", self.lang)),
                view=view)
            return
        username = self.username.value.strip()
        ok, text = await self.cog._run_link(self.member, username,
                                            self.account_type, self.lang)
        view = ResultBackView(self.lang, self.home_factory)
        await interaction.edit_original_response(
            embed=_flow_embed(ok, text), view=view)


class UnlinkConfirmView(discord.ui.View):
    """Confirm removing the whitelist entries + the Discord ↔ MC record."""

    def __init__(self, cog: "Minecraft", lang: str, member: discord.Member,
                 *, home_factory=None, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.member = cog, lang, member
        self.home_factory = home_factory
        self.yes.label = t("mc.unlink.confirm_yes", lang)
        self.back.label = t("mc.hub.back", lang)

    async def embed(self) -> discord.Embed:
        link = self.cog._mc_link_of(await self.cog._record_for(self.member.id))
        if not link:
            return _flow_embed(False, t("mc.unlink.not_linked", self.lang))
        return discord.Embed(
            title=t("mc.unlink.confirm_title", self.lang),
            description=t("mc.unlink.confirm_body", self.lang,
                          name=link["username"]),
            color=discord.Color.red(),
        )

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.danger, row=0)
    async def yes(self, interaction: discord.Interaction,
                  _button: discord.ui.Button):
        await interaction.response.edit_message(embed=_loading_embed(), view=None)
        if not Minecraft._can_manage_link(interaction.user, self.member):
            view = ResultBackView(self.lang, self.home_factory)
            await interaction.message.edit(
                embed=_flow_embed(False, t("mc.link.deny_other", self.lang)),
                view=view)
            return
        ok, text = await self.cog._run_unlink(self.member, self.lang)
        view = ResultBackView(self.lang, self.home_factory)
        await interaction.message.edit(embed=_flow_embed(ok, text), view=view)

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        await _back_to_home(self, interaction)


class ControlView(discord.ui.View):
    """Power control drill-down (operator/Archon only): start / stop / restart."""

    def __init__(self, cog: "Minecraft", lang: str, member: discord.Member,
                 *, home_factory=None, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.member = cog, lang, member
        self.home_factory = home_factory
        self.start.label = t("mc.ctrl.start", lang)
        self.stop.label = t("mc.ctrl.stop", lang)
        self.restart.label = t("mc.ctrl.restart", lang)
        self.back.label = t("mc.hub.back", lang)

    async def embed(self) -> discord.Embed:
        return discord.Embed(
            title=t("mc.hub.control", self.lang),
            description=t("mc.ctrl.choose", self.lang),
            color=discord.Color.blurple(),
        )

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.success, row=0)
    async def start(self, interaction: discord.Interaction,
                    _button: discord.ui.Button):
        await self._run(interaction, "start")

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        await self._run(interaction, "stop")

    @discord.ui.button(emoji="🔄", style=discord.ButtonStyle.primary, row=0)
    async def restart(self, interaction: discord.Interaction,
                      _button: discord.ui.Button):
        await self._run(interaction, "restart")

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        await _back_to_home(self, interaction)

    async def _run(self, interaction: discord.Interaction, signal: str):
        await interaction.response.edit_message(
            embed=_loading_embed(), view=None)
        ok, text = await self.cog._power_core(self.member, self.lang, signal)
        view = ResultBackView(self.lang, self.home_factory)
        await interaction.message.edit(embed=_flow_embed(ok, text), view=view)


class ResultBackView(discord.ui.View):
    """End-of-flow card with a single 'back to menu' button."""

    def __init__(self, lang: str, home_factory=None, *, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.home_factory = home_factory
        self.lang = lang
        self.back.label = t("mc.hub.back", lang)

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        await _back_to_home(self, interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(Minecraft(bot))