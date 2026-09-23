"""mc-link — Discord side of the Discord ↔ Minecraft single sign-on layer.

The robotics_hub TablesDB backend is the source of truth; this bot is its
authenticated client and the ONLY component that mints secrets:

* /mclink mints an account row + a pending pair row (pair_key) and DMs the
  pairing code; the Paper plugin claims it in-game via /mcverify by flipping
  ``is_active`` (one-time pairing, exact-name binding).
* The plugin arms ``minecraft_otp`` rows as "pending mint" (enabled with an
  empty otp_hash) at every join; this watcher fills each with a bcrypt-12
  hash of a fresh uppercase code and DMs the login OTP. The plugin verifies
  the typed code via BCrypt.checkpw, consumes the row and admits the player
  through AuthMe.

Contract: MITIGATION-PLAN §5 of the mc-link plugin repo. Codes are always
DM'd, never posted in guild channels; unclaimed pair rows expire after 5
minutes (bot-side); a user gets at most one fresh OTP per 60 s (join-spam
guard). No IP trust, no auto-login, no shared symmetric secret.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import config
from cogs._mc_crypto import dt_friendly, hash_otp, iso_now, new_link_code, parse_iso
from cogs.minecraft import Minecraft
from data.store import StoreError, _rel_id, store
from i18n.core import resolve_member_lang, t

LOG = logging.getLogger("bot.mclink")

# Pairing codes expire after this long (contract §5.1.6 — 5 minutes).
_PAIR_TTL_SECONDS = config.MC_PAIR_KEY_TTL
# OTP TTL at mint time (contract §5.2.4; plugin default ttl.otp_seconds = 300).
_OTP_TTL_SECONDS = config.MC_LINK_CODE_TTL
# Join-spam guard: at most one fresh OTP per Discord user per window (§5.2.6).
_MINT_COOLDOWN_SECONDS = 60
# Contract §5.1: Minecraft usernames are 3–16 chars of letters/digits/_.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


# ── Cog ────────────────────────────────────────────────────────────────
class McLink(commands.Cog):
    """Pairing (pair_key mint) + per-login OTP minting watcher."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._stop = asyncio.Event()
        self._watch_task: asyncio.Task | None = None
        # discord_id -> epoch seconds of the last OTP minted (join-spam guard).
        self._last_mint: dict[int, float] = {}

    # ── lifecycle ─────────────────────────────────────────────
    def _configured(self) -> bool:
        # The credential handshake is gone: the only secret the bot needs is
        # its Appwrite key (scoped to tablesdb on robotics_hub).
        return bool(config.APPWRITE_API_KEY and config.APPWRITE_ENDPOINT)

    async def cog_load(self) -> None:
        """Start the watcher (OTP minting, pair expiry, activation sync)."""
        if self._configured():
            self._watch_task = asyncio.create_task(self._watch())

    def cog_unload(self) -> None:
        self._stop.set()
        if self._watch_task is not None:
            self._watch_task.cancel()

    async def _lang(self, ctx) -> str:
        locale = str(ctx.interaction.locale) if ctx.interaction else None
        return await resolve_member_lang(ctx.author.id, locale)

    async def _dm(self, user_id: int, content: str,
                  view: discord.ui.View | None = None) -> bool:
        """DM a member; False when DMs are closed / the user can't be found."""
        try:
            user = await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return False
        try:
            if view is not None:
                await user.send(content, view=view)
            else:
                await user.send(content)
            return True
        except discord.HTTPException:
            return False

    def _mc(self) -> Minecraft | None:
        return self.bot.get_cog("Minecraft")  # type: ignore[return-value]

    def _member_in_guilds(self, discord_id: int) -> discord.Member | None:
        for guild in self.bot.guilds:
            member = guild.get_member(discord_id)
            if member is not None:
                return member
        return None

    async def _sync_display_link(self, discord_id: int, username: str) -> None:
        """Keep the member's links.minecraft identity readable by the hub."""
        record = (await store.get_member(discord_id)) or {}
        links = Minecraft._links_of(record)
        links["minecraft"] = {
            "username": username,
            "type": "linked",
            "uuids": [],
            "linked_at": dt_friendly(),
        }
        await store.merge_member(discord_id, {"links": json.dumps(links),
                                              "mc_username": username})

    # ── /mclink ───────────────────────────────────────────────
    @commands.hybrid_command(
        name="mclink",
        description="Link your Minecraft username to Discord via a one-time pairing code.")
    @commands.guild_only()
    @commands.cooldown(2, 60, commands.BucketType.user)
    async def mclink(self, ctx: commands.Context, username: str):
        """Start the one-time pairing: this bot DMs a code typeable in-game.

        The code is minted UPPERCASE from the unambiguous alphabet, is valid
        for ``MC_PAIR_KEY_TTL`` seconds and is never shown in the guild
        channel. The Paper plugin claims it in-game (/mcverify): it creates
        the account's session gateway and flips the pair row to
        ``is_active``. Every later login needs a fresh OTP from a follow-up
        join (this bot's watcher mints and DMs it).
        """
        lang = await self._lang(ctx)
        if not self._configured():
            await ctx.send(t("mc.unconfigured", lang))
            return
        name = (username or "").strip()
        if not _USERNAME_RE.fullmatch(name):
            await ctx.send(t("mclink.bad_name", lang, name=name))
            return
        try:
            # One active link per Discord user (contract §5.1.2).
            if await store.mc_user_linked(ctx.author.id):
                await ctx.send(t("mclink.already_linked", lang))
                return
            # And a name may belong to one account only — refusal is by the
            # active link pointing at it (contract §5.1.2).
            if await store.mc_account_linked(name):
                await ctx.send(t("mclink.taken", lang, name=name))
                return
            pair_key = new_link_code()  # 8× uppercase, unambiguous alphabet
            link_id = await store.mc_create_pair(
                ctx.author.id, name, pair_key,
                username_hint=ctx.author.display_name or ctx.author.name)
        except StoreError as exc:
            LOG.error("mclink: could not create pairing for %s: %s",
                      ctx.author.id, exc)
            await ctx.send(t("mclink.store_fail", lang))
            return
        minutes = max(1, _PAIR_TTL_SECONDS // 60)
        dm_text = (f"{t('mclink.announce', lang)}\n\n"
                   + t("mclink.dm_code", lang, code=pair_key, name=name,
                       minutes=minutes))
        if not await self._dm(ctx.author.id, dm_text):
            # DMs closed → the code is useless; drop the pending pair row so
            # nothing lingers (expiry would catch it anyway).
            try:
                await store.mc_delete_pair(link_id)
            except StoreError:
                pass
            await ctx.send(t("mclink.dm_closed", lang))
            return
        await ctx.send(t("mclink.sent", lang))

    # ── watcher ───────────────────────────────────────────────
    async def _watch(self) -> None:
        """5 s poll loop: OTP minting, pair expiry, activation sync."""
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Only reachable in test harnesses that never connect the client;
            # production always has a live gateway before the watcher spins.
            LOG.warning("mc-link watcher idle: client never started")
            return
        while not self._stop.is_set():
            try:
                await self._watch_cycle()
            except Exception as exc:  # noqa: BLE001 - a bad cycle must never die
                LOG.warning("mc-link watcher cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=config.MC_LINK_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _watch_cycle(self) -> None:
        await self._mint_pending_otps()
        await self._expire_stale_pairs()
        await self._sync_activated_links()

    async def _mint_pending_otps(self) -> None:
        """Fill plugin-armed minecraft_otp rows the plugin left as pending
        mint (``otp_hash == ""``), inside the contract: bcrypt-12 hash, fresh
        TTL, per-user cooldown, never re-mint a row that already has a hash.
        """
        try:
            pending = await store.mc_list_pending_otps()
        except StoreError:
            return
        now_epoch = time.time()
        for row in pending:
            account_id = _rel_id(row.get("minecraft_account"))
            if not account_id:
                continue
            try:
                owner = await store.mc_account_otp_owner(account_id)
            except StoreError:
                continue
            if not owner:
                continue  # no stable active link → nobody to send it to
            username, discord_id = owner
            last = self._last_mint.get(discord_id)
            if last is not None and now_epoch - last < _MINT_COOLDOWN_SECONDS:
                continue  # join-spam guard (§5.2.6)
            code = new_link_code()
            now = datetime.now(timezone.utc)
            try:
                minted = await store.mc_mint_otp(
                    row["$id"],
                    otp_hash=hash_otp(code),
                    otp_salt=secrets.token_hex(16),
                    challenge_at=now.isoformat(),
                    expires_at=(now + timedelta(seconds=_OTP_TTL_SECONDS)
                                ).isoformat())
            except StoreError:
                continue
            if not minted:
                continue  # already minted/consumed by a racing cycle
            self._last_mint[discord_id] = now_epoch
            lang = await resolve_member_lang(discord_id)
            minutes = max(1, _OTP_TTL_SECONDS // 60)
            if not await self._dm(
                    discord_id, t("mclink.dm_otp", lang, code=code,
                                  name=username, minutes=minutes)):
                # DM failed — expire so the plugin re-arms on rejoin instead
                # of telling the player "code on the way" forever.
                try:
                    await store.mc_expire_otp(row["$id"])
                except StoreError:
                    pass

    async def _expire_stale_pairs(self) -> None:
        """Drop unclaimed pair rows older than the 5-minute window (§5.1.6)."""
        try:
            stale = await store.mc_stale_pairs(
                datetime.now(timezone.utc)
                - timedelta(seconds=_PAIR_TTL_SECONDS))
            for row in stale:
                await store.mc_delete_pair(row["$id"])
        except StoreError:
            pass

    async def _sync_activated_links(self) -> None:
        """Post-claim bot work once the plugin flips a link row to active
        (either a /mcverify pair claim or a /linkmc whitelist upsert): MC
        role, member-side display link and a modlog entry.

        Markers make delivery at-least-once across restarts: a link row is
        only processed once its ``$updatedAt`` passes the persisted marker
        (``mc_link.last_claim``, initialized to now on first boot so old
        activations are skipped), and the marker only advances for rows whose
        DM fully succeeded.
        """
        marker = await store.get_setting("mc_link.last_claim") or iso_now()
        marker_dt = parse_iso(marker)
        try:
            links = await store.mc_list_active_links()
        except StoreError:
            return
        latest_raw = marker
        latest_dt = marker_dt
        for link in links:
            updated_raw = str(link.get("$updatedAt") or "")
            updated_dt = parse_iso(updated_raw)
            if updated_dt is None or marker_dt is None \
                    or updated_dt <= marker_dt:
                continue
            if await self._claim_link(link) and (
                    latest_dt is None or updated_dt > latest_dt):
                latest_raw, latest_dt = updated_raw, updated_dt
        if latest_raw != marker:
            await store.set_setting("mc_link.last_claim", latest_raw)

    async def _claim_link(self, link: dict) -> bool:
        account_id = _rel_id(link.get("minecraft_account"))
        discord_id = int(_rel_id(link.get("discord_user")) or 0)
        username = ""
        if account_id:
            try:
                account = await store.mc_resolve_account(account_id)
                username = (account or {}).get("username") or ""
            except StoreError:
                pass
        if not username or not discord_id:
            return True  # malformed — don't retry forever
        lang = await resolve_member_lang(discord_id)
        if not await self._dm(discord_id,
                              t("mclink.dm_thanks", lang, name=username)):
            return False  # DM failed — keep retrying next cycle (at-least-once)
        mc = self._mc()
        member = self._member_in_guilds(discord_id)
        if member is not None and mc is not None:
            try:
                await mc._ensure_mc_role(member)
            except Exception as exc:  # noqa: BLE001 - tagging is best-effort
                LOG.warning("mc-link role assign failed for %s: %s",
                            discord_id, exc)
        try:
            await self._sync_display_link(discord_id, username)
            await store.log_moderation(
                action="mc_link", target_id=discord_id, target_name=username,
                moderator_id=discord_id,
                reason="mc-link pair claimed in-game")
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("mc-link display/audit failed for %s: %s",
                        discord_id, exc)
        return True  # the DM is at-least-once; role/link/audit are idempotent


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(McLink(bot))