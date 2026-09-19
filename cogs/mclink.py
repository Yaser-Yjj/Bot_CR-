"""mc-link — Discord side of the Discord ↔ Minecraft single sign-on layer.

The robotics-ops Appwrite backend is the source of truth; this bot is its
authenticated client and the ONLY component that mints links and credentials.
A Paper plugin (deployed alongside the server) consumes backend state — it
claims link codes and applies bot-issued credentials through AuthMe.

Trust invariant (hard rule, from PLAN.md §0): **Minecraft is an untrusted
client boundary.** Never derive an auth decision from anything a Minecraft
client claims (username, UUID, permissions, "logged in" state). The only real
proofs are (1) Discord identity and (2) knowledge of a secret this bot issued.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import discord
from appwrite.query import Query
from discord.ext import commands

import config
from cogs._mc_crypto import (CipherBox, dt_friendly, iso_now, load_ips,
                             mask_ip, new_link_code, new_temp_password,
                             parse_iso)
from cogs.minecraft import Minecraft, _USERNAME_RE
from data.store import StoreError, store
from i18n.core import resolve_member_lang, t

LOG = logging.getLogger("bot.mclink")


# ── Cog ────────────────────────────────────────────────────────────────
class McLink(commands.Cog):
    """One-time link codes, credential watchers, /mcpass and device control."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._stop = asyncio.Event()
        self._watch_task: asyncio.Task | None = None
        self._cipher: CipherBox | None = None

    # ── lifecycle ─────────────────────────────────────────────
    def _configured(self) -> bool:
        return bool(config.MC_LINK_SECRET)

    def _crypto(self) -> CipherBox:
        if self._cipher is None:
            self._cipher = CipherBox(config.MC_LINK_SECRET)
        return self._cipher

    async def cog_load(self) -> None:
        """Start the code/challenge watchers (mc-link only when configured)."""
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

    async def _linked_username(self, member_id: int) -> str | None:
        """The member's linked Minecraft username (display layer), if any."""
        record = (await store.get_member(member_id)) or {}
        link = Minecraft._mc_link_of(record)
        return (link or {}).get("username")

    async def _sync_display_link(self, discord_id: int, username: str) -> None:
        """Keep bot_members.links.minecraft readable by the legacy display."""
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
        description="Link your Minecraft username to Discord via a one-time code.")
    @commands.guild_only()
    @commands.cooldown(2, 60, commands.BucketType.user)
    async def mclink(self, ctx: commands.Context, username: str):
        """Start the link handshake: this bot DMs a code typeable in-game.

        The code is single-use, expires in ``MC_LINK_CODE_TTL`` seconds, and is
        never shown in the guild channel. The in-game claim is made by the
        Paper plugin against the backend — this command only mints it.
        """
        lang = await self._lang(ctx)
        if not self._configured():
            await ctx.send(t("mc.unconfigured", lang))
            return
        name = (username or "").strip()
        if not _USERNAME_RE.match(name):
            await ctx.send(t("mclink.bad_name", lang, name=name))
            return
        profile = await store.mc_get_auth(name)
        if profile and profile.get("status") == "linked":
            owner = str(profile.get("discord_id") or "")
            if owner and owner != str(ctx.author.id):
                await ctx.send(t("mclink.taken", lang, name=name))
                return
        code = new_link_code()
        now = iso_now()
        expiry = (datetime.now(timezone.utc)
                  + timedelta(seconds=config.MC_LINK_CODE_TTL)).isoformat()
        try:
            doc_id = await store.mc_create("mc_codes", {
                "code": code,
                "username": name,
                "discord_id": str(ctx.author.id),
                "status": "pending",
                "expires_at": expiry,
                "created_at": now,
            })
        except StoreError as exc:
            LOG.error("mclink: could not create code for %s: %s", ctx.author.id, exc)
            await ctx.send(t("mclink.store_fail", lang))
            return
        minutes = max(1, config.MC_LINK_CODE_TTL // 60)
        dm_text = (f"{t('mclink.announce', lang)}\n\n"
                   + t("mclink.dm_code", lang, code=code, name=name,
                       minutes=minutes))
        if not await self._dm(ctx.author.id, dm_text):
            # DMs closed → the code is useless; expire it so nothing lingers.
            try:
                await store.mc_replace("mc_codes", doc_id, {"status": "expired"})
            except StoreError:
                pass
            await ctx.send(t("mclink.dm_closed", lang))
            return
        await ctx.send(t("mclink.sent", lang, minutes=minutes))

    # ── watchers ──────────────────────────────────────────────
    async def _watch(self) -> None:
        """5 s poll loop: link-code claims and credential-challenge jobs."""
        await self.bot.wait_until_ready()
        while not self._stop.is_set():
            try:
                await self._watch_codes()
                await self._watch_challenges()
            except Exception as exc:  # noqa: BLE001 - a bad cycle must never die
                LOG.warning("mc-link watcher cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=config.MC_LINK_POLL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _watch_codes(self) -> None:
        """Mint-less follow-up on claims: thanks DM, role, display link, audit.

        Markers make delivery at-least-once across restarts: a doc is only
        processed once its ``$updatedAt`` passes the persisted marker, and the
        marker only advances for hands that fully succeeded (DM sent).
        """
        marker = await store.get_setting("mc_link.last_code") or iso_now()
        marker_dt = parse_iso(marker)
        try:
            docs = await store.mc_list(
                "mc_codes", [Query.equal("status", "used")], limit=50)
        except StoreError:
            return
        latest_raw = marker
        latest_dt = marker_dt
        for doc in docs:
            updated_raw = str(doc.get("$updatedAt") or "")
            updated_dt = parse_iso(updated_raw)
            if updated_dt is None or marker_dt is None \
                    or updated_dt <= marker_dt:
                continue
            if await self._claim_code(doc) and (
                    latest_dt is None or updated_dt > latest_dt):
                latest_raw, latest_dt = updated_raw, updated_dt
        if latest_raw != marker:
            await store.set_setting("mc_link.last_code", latest_raw)

    async def _claim_code(self, doc: dict) -> bool:
        username = doc.get("username") or ""
        discord_id = int(doc.get("discord_id") or 0)
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
                reason=f"mc-link code claimed (code {doc.get('code') or '?'})")
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            LOG.warning("mc-link display/audit failed for %s: %s",
                        discord_id, exc)
        return True  # the DM is at-least-once; role/link/audit are idempotent

    async def _watch_challenges(self) -> None:
        marker = (await store.get_setting("mc_link.last_challenge")
                  or iso_now())
        marker_dt = parse_iso(marker)
        try:
            docs = await store.mc_list("mc_challenges", [], limit=100)
        except StoreError:
            return
        latest_raw = marker
        latest_dt = marker_dt
        for doc in docs:
            updated_raw = str(doc.get("$updatedAt") or "")
            updated_dt = parse_iso(updated_raw)
            if updated_dt is None or marker_dt is None \
                    or updated_dt <= marker_dt:
                continue
            kind = doc.get("kind") or ""
            status = doc.get("status") or ""
            handled = False
            if kind == "new_ip":
                handled = await self._handle_new_ip(doc)
            elif kind == "change_password":
                if status == "done":
                    handled = await self._notify_pw_status(doc, ok=True)
                elif status == "failed":
                    handled = await self._notify_pw_status(doc, ok=False)
            if handled and (latest_dt is None or updated_dt > latest_dt):
                latest_raw, latest_dt = updated_raw, updated_dt
        if latest_raw != marker:
            await store.set_setting("mc_link.last_challenge", latest_raw)

    async def _handle_new_ip(self, doc: dict) -> bool:
        """Mint + DM the one-time temp password for an unknown-IP login.

        Pending → seal + approve, then DM. If the DM fails the doc stays
        approved *after* the marker, so the next cycle retries delivery (the
        temp password is valid for the whole TTL).
        """
        username = doc.get("username") or ""
        if not username:
            return True
        doc_id = doc.get("$id")
        if doc.get("status") == "pending":
            temp = new_temp_password()
            token = self._crypto().seal(username, temp)
            try:
                await store.mc_replace("mc_challenges", doc_id,
                                       {"payload_enc": token,
                                        "status": "approved"})
            except StoreError:
                return False
        else:  # approved but undelivered before -> recover the temp password
            try:
                temp = self._crypto().open(username,
                                           doc.get("payload_enc") or "")
            except Exception:  # noqa: BLE001 - undecryptable = dead challenge
                LOG.warning("mc-link: cannot open approved challenge for %s",
                            username)
                return True
        profile = await store.mc_get_auth(username)
        discord_id = int((profile or {}).get("discord_id") or 0)
        if not discord_id:
            return True  # profile gone — nothing to deliver to
        minutes = max(1, config.MC_TEMP_TTL // 60)
        lang = await resolve_member_lang(discord_id)
        view = NewIpDenyView(self, doc_id, discord_id, lang)
        content = t("mclink.challenge.new_ip", lang,
                    ip=doc.get("ip") or "?", pw=temp, minutes=minutes)
        return await self._dm(discord_id, content, view=view)

    async def _notify_pw_status(self, doc: dict, *, ok: bool) -> bool:
        username = doc.get("username") or ""
        profile = await store.mc_get_auth(username)
        discord_id = int((profile or {}).get("discord_id") or 0)
        if not discord_id:
            return True
        lang = await resolve_member_lang(discord_id)
        key = "mclink.challenge.pw_done" if ok else "mclink.challenge.pw_failed"
        return await self._dm(discord_id, t(key, lang, name=username))

    # ── /mcpass ───────────────────────────────────────────────
    @commands.hybrid_command(name="mcpass",
                             description="Change your Minecraft server password.")
    @commands.guild_only()
    async def mcpass(self, ctx: commands.Context):
        lang = await self._lang(ctx)
        if not self._configured():
            await ctx.send(t("mc.unconfigured", lang))
            return
        username = await self._linked_username(ctx.author.id)
        if not username:
            await ctx.send(t("mclink.no_link", lang))
            return
        if ctx.interaction is None:
            await ctx.send(t("mcpass.prefix_hint", lang))
            return
        await ctx.interaction.response.send_modal(
            McPassModal(self, lang, username))

    async def open_mcpass_modal(self, interaction: discord.Interaction,
                                lang: str, username: str) -> None:
        """Shared entry for the profile/hub "Change password" button."""
        profile = await store.mc_get_auth(username)
        if profile is None:
            embed = discord.Embed(
                title=t("mcpass.modal_title", lang),
                description=t("mclink.no_link", lang),
                color=discord.Color.red())
            await interaction.response.edit_message(embed=embed, view=None)
            return
        if not _device_access_ok(interaction, profile):
            await interaction.response.send_message(
                t("devices.denied", lang), ephemeral=True)
            return
        await interaction.response.send_modal(
            McPassModal(self, lang, username))

    # ── devices ───────────────────────────────────────────────
    async def open_devices_view(self, interaction: discord.Interaction,
                                lang: str, username: str,
                                home_factory=None) -> None:
        """Shared entry for the profile/hub "Devices" button (F7)."""
        profile = await store.mc_get_auth(username)
        if profile is None:
            embed = discord.Embed(
                title=t("devices.title", lang),
                description=t("mclink.no_link", lang),
                color=discord.Color.red())
            await interaction.response.edit_message(embed=embed, view=None)
            return
        view = DevicesView(self, lang, username, profile,
                           home_factory=home_factory)
        embed = await view.embed()
        await interaction.response.edit_message(embed=embed, view=view)


# ── Modal: /mcpass ─────────────────────────────────────────────────────
class McPassModal(discord.ui.Modal):
    """New password (8–64 chars) with a confirm field (F5)."""

    def __init__(self, cog: McLink, lang: str, username: str):
        super().__init__(title=t("mcpass.modal_title", lang))
        self.cog, self.lang, self.username = cog, lang, username
        self.pw = discord.ui.TextInput(
            label=t("mcpass.modal_pw", lang),
            placeholder=t("mcpass.modal_pw_ph", lang),
            min_length=8, max_length=64, required=True)
        self.confirm = discord.ui.TextInput(
            label=t("mcpass.modal_confirm", lang),
            placeholder=t("mcpass.modal_confirm_ph", lang),
            min_length=8, max_length=64, required=True)
        self.add_item(self.pw)
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.pw.value != self.confirm.value:
            await interaction.response.send_message(
                t("mcpass.mismatch", self.lang), ephemeral=True)
            return
        try:
            token = self.cog._crypto().seal(self.username, self.pw.value)
            await store.mc_create("mc_challenges", {
                "username": self.username,
                "kind": "change_password",
                "status": "pending",
                "payload_enc": token,
                "created_at": iso_now(),
                "expires_at": (datetime.now(timezone.utc)
                               + timedelta(minutes=10)).isoformat(),
            })
        except (StoreError, ValueError) as exc:
            LOG.error("mcpass: could not create challenge for %s: %s",
                      self.username, exc)
            await interaction.response.send_message(
                t("mcpass.store_fail", self.lang), ephemeral=True)
            return
        await interaction.response.send_message(
            t("mcpass.applying", self.lang), ephemeral=True)


# ── Deny button in the new-IP DM ───────────────────────────────────────
class NewIpDenyView(discord.ui.View):
    """In-DM 'Deny' for a new-IP challenge: mark it failed (F4)."""

    def __init__(self, cog: McLink, challenge_id: str, discord_id: int,
                 lang: str, *, timeout: float = 600.0):
        super().__init__(timeout=timeout)
        self.cog, self.challenge_id, self.discord_id = (
            cog, challenge_id, discord_id)
        self.lang = lang
        self.deny.label = t("mclink.challenge.deny", lang)
        self.fine.label = t("mclink.challenge.deny_fine", lang)

    @discord.ui.button(emoji="❌", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message(
                t("mclink.challenge.not_yours", self.lang), ephemeral=True)
            return
        try:
            await store.mc_replace("mc_challenges", self.challenge_id,
                                   {"status": "failed"})
        except StoreError as exc:
            LOG.warning("new-IP deny failed: %s", exc)
            await interaction.response.send_message(
                t("mclink.challenge.deny_failed", self.lang),
                ephemeral=True)
            return
        await interaction.response.edit_message(
            content=t("mclink.challenge.denied", self.lang),
            embed=None, view=None)

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.secondary)
    async def fine(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        if interaction.user.id != self.discord_id:
            await interaction.response.send_message(
                t("mclink.challenge.not_yours", self.lang), ephemeral=True)
            return
        await interaction.response.edit_message(
            content=t("mclink.challenge.confirmed", self.lang),
            embed=None, view=None)


# ── Devices drill-down (F7) ────────────────────────────────────────────
def _device_access_ok(interaction: discord.Interaction, profile: dict) -> bool:
    """Only the linked member (or an MC operator) may view/manage devices.

    Device IPs are privacy-sensitive even inside the hub/profile — a button
    left in a shared channel must never leak the list to random members.
    ``profile`` is the ``mc_auth`` doc (its ``discord_id`` is the owner).
    """
    owner = str(profile.get("discord_id") or "")
    if owner and str(interaction.user.id) == owner:
        return True
    if getattr(interaction.user, "roles", None) is None:
        return False
    try:
        return bool(Minecraft._is_mc_operator(interaction.user))  # type: ignore[arg-type]
    except Exception:
        return False


class DevicesView(discord.ui.View):
    """IP list for a linked username: current session + per-IP drill-down."""

    def __init__(self, cog: McLink, lang: str, username: str,
                 profile: dict, *, home_factory=None, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.username = cog, lang, username
        self.profile = profile
        self.home_factory = home_factory
        self.back.label = t("devices.back", lang)
        rows = load_ips(profile.get("last_ips"))
        for i, row in enumerate(rows[:10]):
            ip = row.get("ip") or "?"
            date = (row.get("seen_at") or "")[:10]
            label = f"{mask_ip(ip)}{f'  ·  {date}' if date else ''}"
            row_btn = discord.ui.Button(
                label=label, style=discord.ButtonStyle.secondary,
                row=i // 5)
            # Button.callback receives a single interaction argument.
            row_btn.callback = self._row_cb(row)
            self.add_item(row_btn)

    def _row_cb(self, row: dict):
        async def _cb(interaction: discord.Interaction) -> None:
            if not _device_access_ok(interaction, self.profile):
                await interaction.response.send_message(
                    t("devices.denied", self.lang), ephemeral=True)
                return
            view = IpDetailView(self.cog, self.lang, self.username,
                                self.profile, row,
                                home_factory=self.home_factory)
            await interaction.response.edit_message(embed=await view.embed(),
                                                    view=view)
        return _cb

    async def embed(self) -> discord.Embed:
        rows = load_ips(self.profile.get("last_ips"))
        embed = discord.Embed(title=t("devices.title", self.lang),
                              color=0x55AA55)
        current = self.profile.get("current_ip")
        embed.add_field(
            name=t("devices.current", self.lang),
            value=f"`{mask_ip(current)}`" if current
            else t("devices.current_none", self.lang),
            inline=False)
        if rows:
            lines = "\n".join(
                f"• `{mask_ip(r.get('ip') or '?')}`  ·  "
                f"{(r.get('seen_at') or '')[:10]}"
                for r in rows)
            embed.add_field(name=t("devices.list", self.lang, n=len(rows)),
                            value=lines, inline=False)
        else:
            embed.add_field(name=t("devices.list", self.lang, n=0),
                            value=t("devices.list_empty", self.lang),
                            inline=False)
        embed.set_footer(text=t("devices.hint", self.lang))
        return embed

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        if not _device_access_ok(interaction, self.profile):
            await interaction.response.send_message(
                t("devices.denied", self.lang), ephemeral=True)
            return
        from cogs.minecraft import _back_to_home
        await _back_to_home(self, interaction)


class IpDetailView(discord.ui.View):
    """One IP row: raw address on detail + remove-with-confirm (F7)."""

    def __init__(self, cog: McLink, lang: str, username: str,
                 profile: dict, row: dict, *, home_factory=None,
                 timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.username = cog, lang, username
        self.profile, self.row = profile, row
        self.home_factory = home_factory
        self.remove.label = t("devices.remove", lang)
        self.back.label = t("devices.back", lang)

    async def embed(self) -> discord.Embed:
        ip = self.row.get("ip") or "?"
        seen = (self.row.get("seen_at") or "")[:16]
        embed = discord.Embed(title=t("devices.row_title", self.lang),
                              color=0x55AA55)
        embed.add_field(name=t("devices.detail_ip", self.lang),
                        value=f"`{ip}`", inline=False)
        if seen:
            embed.add_field(name=t("devices.detail_seen", self.lang),
                            value=seen, inline=False)
        if ip and ip == (self.profile.get("current_ip") or ""):
            embed.add_field(name=t("devices.current", self.lang),
                            value=t("devices.session_current", self.lang),
                            inline=False)
        embed.set_footer(text=t("devices.detail_hint", self.lang))
        return embed

    @discord.ui.button(emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction,
                     _button: discord.ui.Button):
        if not _device_access_ok(interaction, self.profile):
            await interaction.response.send_message(
                t("devices.denied", self.lang), ephemeral=True)
            return
        view = RemoveIpConfirmView(self.cog, self.lang, self.username,
                                   self.row, self.profile,
                                   home_factory=self.home_factory)
        embed = await view.embed()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction,
                   _button: discord.ui.Button):
        if not _device_access_ok(interaction, self.profile):
            await interaction.response.send_message(
                t("devices.denied", self.lang), ephemeral=True)
            return
        profile = await store.mc_get_auth(self.username) or self.profile
        view = DevicesView(self.cog, self.lang, self.username, profile,
                           home_factory=self.home_factory)
        await interaction.response.edit_message(embed=await view.embed(),
                                                view=view)


class RemoveIpConfirmView(discord.ui.View):
    """Confirm step before an IP is dropped from mc_auth.last_ips."""

    def __init__(self, cog: McLink, lang: str, username: str, row: dict,
                 profile: dict, *, home_factory=None, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.cog, self.lang, self.username = cog, lang, username
        self.row, self.profile = row, profile
        self.home_factory = home_factory
        self.yes.label = t("devices.remove_confirm", lang)
        self.no.label = t("devices.cancel", lang)

    async def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=t("devices.remove_title", self.lang),
            description=t("devices.remove_body", self.lang,
                          ip=mask_ip(self.row.get("ip") or "?")),
            color=0xCC5555)
        embed.set_footer(text=t("devices.remove_foot", self.lang))
        return embed

    @discord.ui.button(emoji="✅", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction,
                  _button: discord.ui.Button):
        if not _device_access_ok(interaction, self.profile):
            await interaction.response.send_message(
                t("devices.denied", self.lang), ephemeral=True)
            return
        username = self.username
        fresh = await store.mc_get_auth(username) or self.profile
        rows = load_ips(fresh.get("last_ips"))
        target = self.row.get("ip")
        kept = [r for r in rows if (r.get("ip") or "") != target]
        try:
            await store.mc_save_auth(username, {"last_ips": json.dumps(kept)})
            fresh = await store.mc_get_auth(username) or {
                **fresh, "last_ips": json.dumps(kept)}
        except StoreError as exc:
            LOG.warning("devices: remove ip failed for %s: %s", username, exc)
            view = DevicesView(self.cog, self.lang, username, fresh,
                               home_factory=self.home_factory)
            embed = await view.embed()
            embed.description = t("devices.remove_fail", self.lang)
            await interaction.response.edit_message(embed=embed, view=view)
            return
        confirmed = discord.Embed(
            description=t("devices.removed", self.lang,
                          ip=mask_ip(target or "?")),
            color=0x55AA55)
        await interaction.response.edit_message(embed=confirmed, view=None)
        # Briefly show the confirmation, then hand back to the refreshed list.
        view = DevicesView(self.cog, self.lang, username, fresh,
                           home_factory=self.home_factory)
        await asyncio.sleep(1.25)
        try:
            await interaction.message.edit(embed=await view.embed(), view=view)
        except discord.HTTPException:
            pass

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction,
                 _button: discord.ui.Button):
        if not _device_access_ok(interaction, self.profile):
            await interaction.response.send_message(
                t("devices.denied", self.lang), ephemeral=True)
            return
        profile = await store.mc_get_auth(self.username) or self.profile
        view = DevicesView(self.cog, self.lang, self.username, profile,
                           home_factory=self.home_factory)
        await interaction.response.edit_message(embed=await view.embed(),
                                                view=view)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(McLink(bot))