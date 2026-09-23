"""Public polls for Bot_CR — transparent or anonymous, stored in Appwrite.

Polls live in the ``bot_polls`` collection, so the club dashboard (which shares
the same Appwrite backend) sees every poll, every vote and every timestamp
without any manual bookkeeping.

Transparent polls : anyone can see counts AND who voted for what, at any time
                    (e.g. "who is going to the competition" — clean attendance).
Anonymous polls   : the voter is stored as a hash of user+poll, never the raw
                    Discord ID, so results can only ever show counts
                    (e.g. secret ballot for the new Chiefs). Optionally the
                    counts stay hidden until staff close the poll.

Every vote records its own timestamp; creating and closing the poll stamp
``created_at`` / ``closed_at``.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs._perms import is_bot_admin
from data.store import StoreError
from data.store import store

LOG = logging.getLogger("bot.polls")

MAX_OPTIONS = 12

MODE_CHOICES = [
    app_commands.Choice(name="Transparent — everyone sees who voted",
                        value="transparent"),
    app_commands.Choice(name="Anonymous — votes stay secret",
                        value="anonymous"),
]
SELECTION_CHOICES = [
    app_commands.Choice(name="Single choice", value="single"),
    app_commands.Choice(name="Multiple choice", value="multiple"),
]


# ── helpers ───────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_dt(value) -> str | None:
    """Relative Discord timestamp ("in 2 hours"), or None when unparsable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    return discord.utils.format_dt(dt, style="R")


def _choice_value(value) -> str:
    """Choice params come as Choice via slash and as raw text via prefix."""
    if isinstance(value, app_commands.Choice):
        return value.value
    return (value or "").strip().lower()


def _voter_id(mode: str, poll_id: str, user_id: int) -> str:
    """Identity stored with a vote.

    Transparent polls store ``u:<user id>`` so results can name voters.
    Anonymous polls store ``h:<sha256(poll:user)>`` so the raw database never
    reveals who voted — only the bot can recompute/verify the hash.
    """
    if mode == "anonymous":
        digest = hashlib.sha256(f"{poll_id}:{user_id}".encode()).hexdigest()[:24]
        return f"h:{digest}"
    return f"u:{user_id}"


def _vote_payload(mode: str, poll_id: str, user_id: int, option: int) -> dict:
    return {"v": _voter_id(mode, poll_id, user_id), "i": option, "t": _now()}


def _encode_votes(votes: list[dict]) -> list[str]:
    """Serialize votes to the JSON strings stored in Appwrite's array."""
    return [json.dumps(v, separators=(",", ":"), sort_keys=True) for v in votes]


def _parse_votes(raw) -> list[dict]:
    out = []
    for entry in raw or []:
        try:
            v = json.loads(str(entry))
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict) and isinstance(v.get("i"), int):
            out.append(v)
    return out


def _counts(options: list[str], votes: list[dict]) -> list[int]:
    counts = [0] * len(options)
    for v in votes:
        i = v.get("i")
        if isinstance(i, int) and 0 <= i < len(counts):
            counts[i] += 1
    return counts


def _option_index(option: str, options: list[str]) -> int | None:
    """Resolve "1" (number) or the exact option text to an index."""
    o = (option or "").strip()
    if o.isdigit():
        i = int(o) - 1
        return i if 0 <= i < len(options) else None
    low = o.lower()
    for i, opt in enumerate(options):
        if low == (opt or "").strip().lower():
            return i
    return None


def _badges(poll: dict) -> str:
    mode = poll.get("mode") or "transparent"
    selection = poll.get("selection") or "single"
    parts = [
        "🕶️ anonymous" if mode == "anonymous" else "🔓 transparent",
        "☝ single choice" if selection == "single" else "🎚️ multiple choice",
    ]
    return " · ".join(parts)


def _poll_embed(poll: dict) -> discord.Embed:
    opts = poll.get("options") or []
    counts = _counts(opts, _parse_votes(poll.get("votes")))
    total = sum(counts)
    closed = bool(poll.get("closed"))
    hidden = bool(poll.get("hide_results")) and not closed
    anonymous = (poll.get("mode") or "transparent") == "anonymous"
    description = f"`{poll.get('poll_id')}` · {_badges(poll)}"
    if not hidden:
        description += f" · **{total}** vote" + ("" if total == 1 else "s")
    embed = discord.Embed(
        title=f"🗳️ {poll.get('question') or 'Untitled poll'}",
        description=description,
        color=discord.Color.dark_grey() if closed else discord.Color.dark_blue(),
    )
    for i, opt in enumerate(opts):
        if hidden:
            value = "🔒 results hidden until close"
        elif anonymous and not closed:
            value = "🔏 per-option counts stay private until close"
        else:
            value = f"👥 {counts[i]} vote" + ("" if counts[i] == 1 else "s")
        embed.add_field(name=f"{i + 1}. {opt}", value=value, inline=False)
    status = "🔒 closed" if closed else "🔓 open"
    if hidden:
        hint = "results hidden"
    elif anonymous and not closed:
        hint = "aggregate total only · per-option hidden"
    else:
        hint = "tap a button to vote"
    embed.set_footer(
        text=(f"{status} · {hint} · Results: /poll results {poll.get('poll_id')}")
    )
    return embed


def _results_embed(poll: dict, ctx) -> discord.Embed:
    opts = poll.get("options") or []
    votes = _parse_votes(poll.get("votes"))
    counts = _counts(opts, votes)
    total = sum(counts)
    anonymous = (poll.get("mode") or "transparent") == "anonymous"
    closed = bool(poll.get("closed"))
    embed = discord.Embed(
        title=f"📊 Results — {poll.get('question') or 'Untitled poll'}",
        description=(f"`{poll.get('poll_id')}` · {_badges(poll)}"
                     f" · **{total}** vote" + ("" if total == 1 else "s")),
        color=discord.Color.green() if closed else discord.Color.blurple(),
    )
    by_option: list[list[dict]] = [[] for _ in opts]
    for v in votes:
        i = v.get("i")
        if isinstance(i, int) and 0 <= i < len(opts):
            by_option[i].append(v)
    for i, opt in enumerate(opts):
        c = counts[i]
        pct = round(c * 100 / total) if total else 0
        bar = "█" * (pct // 10) + "░" * (10 - (pct // 10))
        value = f"{bar} {c} ({pct}%)"
        if not anonymous:
            who = []
            for v in by_option[i]:
                vid = v.get("v") or ""
                if vid.startswith("u:"):
                    mid = vid[2:]
                    name = vid
                    if mid.isdigit():
                        member = ctx.guild.get_member(int(mid))
                        name = member.display_name if member else f"<@{mid}>"
                    ts = _fmt_dt(v.get("t"))
                    who.append(f"{name}" + (f" ({ts})" if ts else ""))
            if who:
                shown = ", ".join(who[:12])
                if len(who) > 12:
                    shown += f" +{len(who) - 12} more"
                value += f"\n└ {shown}"
        embed.add_field(name=f"{i + 1}. {opt}", value=value, inline=False)
    created = _fmt_dt(poll.get("created_at"))
    closed_at = _fmt_dt(poll.get("closed_at"))
    footer = (f"Created {created}" if created else "")
    if closed_at:
        footer += (f" · " if footer else "") + f"Closed {closed_at}"
    if footer:
        embed.set_footer(text=footer)
    return embed


class PollButton(discord.ui.Button):
    """One option on a poll; identity is carried in a stable ``custom_id``.

    ``poll:{poll_id}:{index}`` lets the button survive the bot restarting —
    the cog re-registers these views on ready, so buttons on messages posted
    days ago keep working.
    """

    def __init__(self, poll_id: str, index: int, label: str, row: int):
        super().__init__(label=label, style=discord.ButtonStyle.secondary,
                         custom_id=f"poll:{poll_id}:{index}", row=row)
        self.poll_id = poll_id
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        if isinstance(self.view, PollVoteView):
            await self.view.handle_vote(interaction, self.index)


class PollVoteView(discord.ui.View):
    """Interactive vote buttons — one per option, persistent across restarts."""

    def __init__(self, cog, poll: dict):
        super().__init__(timeout=None)
        self.cog = cog
        self.poll_id = str(poll.get("poll_id") or "")
        for i, opt in enumerate(poll.get("options") or []):
            label = (opt or "").strip()
            if len(label) > 80:
                label = label[:77] + "…"
            self.add_item(PollButton(self.poll_id, i, label, row=i // 5))

    def _closed_view(self) -> "PollVoteView":
        for child in self.children:
            child.disabled = True
        return self

    async def handle_vote(self, interaction: discord.Interaction, idx: int):
        """Apply a vote (same semantics as /poll vote) and refresh the embed."""
        try:
            poll = (await store.get_poll(self.poll_id)) or {}
        except StoreError:
            await interaction.response.send_message(
                "⚠️ Couldn't load that poll right now.", ephemeral=True)
            return
        opts = poll.get("options") or []
        if not opts or not (0 <= idx < len(opts)):
            await interaction.response.send_message(
                "⚠️ That option no longer exists.", ephemeral=True)
            return
        if poll.get("closed"):
            await interaction.response.edit_message(
                embed=_poll_embed(poll), view=self._closed_view())
            await interaction.followup.send(
                f"🔒 **{poll.get('question')}** is closed — voting is locked.",
                ephemeral=True)
            return

        mode = poll.get("mode") or "transparent"
        vid = _voter_id(mode, self.poll_id, interaction.user.id)
        votes = _parse_votes(poll.get("votes"))
        single = (poll.get("selection") or "single") != "multiple"

        if single:
            prior = [v for v in votes if v.get("v") == vid]
            votes = [v for v in votes if v.get("v") != vid]
            if prior and prior[0].get("i") == idx:
                msg = (f"🗳️ You already voted **{opts[idx]}** on "
                       f"**{poll.get('question')}** — that vote was removed.")
            elif prior:
                votes.append(_vote_payload(mode, self.poll_id,
                                           interaction.user.id, idx))
                old = opts[prior[0].get("i")]
                msg = (f"🗳️ You already voted **{old}** on **{poll.get('question')}**"
                       f" — your vote has been **changed** to **{opts[idx]}**.")
            else:
                votes.append(_vote_payload(mode, self.poll_id,
                                           interaction.user.id, idx))
                msg = f"🗳️ You voted **{opts[idx]}**."
        else:
            before = len(votes)
            votes = [v for v in votes
                     if not (v.get("v") == vid and v.get("i") == idx)]
            if len(votes) == before:
                votes.append(_vote_payload(mode, self.poll_id,
                                           interaction.user.id, idx))
                msg = f"🗳️ You voted **{opts[idx]}** (+1 choice)."
            else:
                msg = f"🗳️ Your vote on **{opts[idx]}** was removed (toggle)."

        poll["votes"] = _encode_votes(votes)
        try:
            await store.save_poll(self.poll_id, poll)
        except StoreError as exc:
            await interaction.response.send_message(
                f"⚠️ Couldn't save your vote: {exc}", ephemeral=True)
            return
        LOG.info("Poll %s: %s voted option %d via button (%s)",
                 self.poll_id, interaction.user, idx, mode)
        await interaction.response.edit_message(embed=_poll_embed(poll), view=self)


class Polls(commands.Cog):
    """🗳️ Public polls — transparent or anonymous, stored in Appwrite."""

    def __init__(self, bot):
        self.bot = bot
        self._views_registered = False

    # ── helpers ─────────────────────────────────────────────────
    async def _find_poll(self, poll_id: str) -> dict | None:
        poll_id = (poll_id or "").strip().upper()
        if not poll_id:
            return None
        try:
            return await store.get_poll(poll_id)
        except StoreError:
            return None

    async def _require_poll(self, ctx, poll_id: str) -> dict | None:
        poll = await self._find_poll(poll_id)
        if poll is None:
            await ctx.send("⚠️ No poll with that id. Use `/poll list` to see the polls.")
            return None
        return poll

    async def _send_poll_list(self, ctx):
        try:
            polls = await store.list_polls()
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't load polls: {exc}")
            return
        if not polls:
            await ctx.send("🗳️ No polls yet. Start one:\n"
                           "`/poll create \"Which comp?\" \"RoboCup|WRO|FIRST\"`")
            return
        lines = []
        for p in polls[:15]:
            votes = _parse_votes(p.get("votes"))
            counts = _counts(p.get("options") or [], votes)
            status = "🔒 closed" if p.get("closed") else "🔓 open"
            badge = "🕶️" if (p.get("mode") or "transparent") == "anonymous" else "👀"
            total = sum(counts)
            ts = _fmt_dt(p.get("created_at"))
            lines.append(
                f"{status} `{p.get('poll_id')}` · {badge} **{p.get('question')}** · "
                f"{total} vote" + ("s" if total != 1 else "") + (f" · {ts}" if ts else "")
            )
        embed = discord.Embed(
            title="🗳️ Club polls",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text="Tap a button on the poll to vote · Results: /poll results <id>"
        )
        await ctx.send(embed=embed)

    # ── the /poll group ─────────────────────────────────────────
    @commands.hybrid_group(name="poll",
                           description="Create & vote in club polls.",
                           invoke_without_command=True)
    @commands.guild_only()
    async def poll(self, ctx):
        """Bare /poll shows an overview of every poll."""
        await self._send_poll_list(ctx)

    @poll.command(name="list", description="List every poll.")
    @commands.guild_only()
    async def poll_list(self, ctx):
        await self._send_poll_list(ctx)

    @poll.command(name="create",
                  description="Create a poll. Separate options with |.")
    @commands.guild_only()
    async def poll_create(self, ctx, question: str, options: str,
                          mode: app_commands.Choice[str] = None,
                          selection: app_commands.Choice[str] = None,
                          hide_results: bool = False):
        """Options are separated by |, e.g. RoboCup|WRO|FIRST."""
        question = (question or "").strip()
        if not question:
            await ctx.send("⚠️ Give the poll a question.")
            return
        opts = [o.strip() for o in (options or "").split("|") if o.strip()]
        if len(opts) < 2:
            await ctx.send("⚠️ A poll needs at least 2 options, separated by `|`:\n"
                           "`/poll create \"Question?\" \"Option A|Option B\"`")
            return
        if len(opts) > MAX_OPTIONS:
            await ctx.send(f"⚠️ Keep it to {MAX_OPTIONS} options max.")
            return
        mode_val = _choice_value(mode) or "transparent"
        if mode_val not in ("transparent", "anonymous"):
            mode_val = "transparent"
        selection_val = _choice_value(selection) or "single"
        if selection_val not in ("single", "multiple"):
            selection_val = "single"
        if hide_results and mode_val != "anonymous":
            # Only secret ballots benefit from hiding live counts.
            mode_val = "anonymous"

        poll_id = await store.next_poll_code()
        payload = {
            "poll_id": poll_id,
            "question": question,
            "options": opts,
            "mode": mode_val,
            "selection": selection_val,
            "hide_results": bool(hide_results),
            "closed": False,
            "closed_at": None,
            "created_by": str(ctx.author.id),
            "created_at": _now(),
            "votes": [],
        }
        try:
            await store.save_poll(poll_id, payload)
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't create the poll: {exc}")
            return
        LOG.info("Poll %s created by %s (%s)", poll_id, ctx.author, mode_val)
        await ctx.send(embed=_poll_embed(payload), view=PollVoteView(self, payload))

    @poll.command(name="vote",
                  description="Vote (same option again = undo your vote).")
    @commands.guild_only()
    async def poll_vote(self, ctx, poll: str, option: str):
        """Single-choice: replaces your vote. Multiple-choice: toggles."""
        p = await self._require_poll(ctx, poll)
        if p is None:
            return
        poll_id = p.get("poll_id")
        if p.get("closed"):
            await ctx.send(f"🔒 **{p.get('question')}** is closed — votes are locked.")
            return
        opts = p.get("options") or []
        idx = _option_index(option, opts)
        if idx is None:
            await ctx.send(f"⚠️ `{option}` isn't a valid option — pass the number "
                           f"(e.g. `1`) or the exact text. `/poll results {poll_id}` lists them.")
            return
        mode = p.get("mode") or "transparent"
        vid = _voter_id(mode, poll_id, ctx.author.id)
        votes = _parse_votes(p.get("votes"))
        single = (p.get("selection") or "single") != "multiple"

        if single:
            prior = [v for v in votes if v.get("v") == vid]
            votes = [v for v in votes if v.get("v") != vid]
            if prior and prior[0].get("i") == idx:
                msg = (f"🗳️ You already voted **{opts[idx]}** on "
                       f"**{p.get('question')}** — that vote was removed.")
            elif prior:
                votes.append(_vote_payload(mode, poll_id, ctx.author.id, idx))
                old = opts[prior[0].get("i")]
                msg = (f"🗳️ You already voted **{old}** on **{p.get('question')}**"
                       f" — your vote has been **changed** to **{opts[idx]}**.")
            else:
                votes.append(_vote_payload(mode, poll_id, ctx.author.id, idx))
                msg = f"🗳️ You voted **{opts[idx]}** on **{p.get('question')}**."
        else:
            before = len(votes)
            votes = [v for v in votes
                     if not (v.get("v") == vid and v.get("i") == idx)]
            if len(votes) == before:
                votes.append(_vote_payload(mode, poll_id, ctx.author.id, idx))
                msg = f"🗳️ You voted **{opts[idx]}** (+1 choice) on **{p.get('question')}**."
            else:
                msg = f"🗳️ Your vote on **{opts[idx]}** was removed (toggle)."

        p["votes"] = _encode_votes(votes)
        try:
            await store.save_poll(poll_id, p)
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't save your vote: {exc}")
            return
        LOG.info("Poll %s: %s voted option %d (%s)", poll_id, ctx.author, idx, mode)
        await ctx.send(msg)

    @poll.command(name="results",
                  description="Show results (anonymous polls hide the voters).")
    @commands.guild_only()
    async def poll_results(self, ctx, poll: str):
        p = await self._require_poll(ctx, poll)
        if p is None:
            return
        if p.get("hide_results") and not p.get("closed"):
            await ctx.send("🔒 This ballot is secret — results unlock when staff "
                           f"close it (`/poll close {p.get('poll_id')}`).")
            return
        await ctx.send(embed=_results_embed(p, ctx))

    @poll.command(name="close",
                  description="Close a poll (staff or the poll creator).")
    @commands.guild_only()
    async def poll_close(self, ctx, poll: str):
        p = await self._require_poll(ctx, poll)
        if p is None:
            return
        if p.get("closed"):
            await ctx.send(f"🔒 **{p.get('question')}** is already closed.")
            return
        if not (is_bot_admin(ctx.author)
                or str(p.get("created_by")) == str(ctx.author.id)):
            await ctx.send("⛔ Only staff, or the member who created this poll, can close it.")
            return
        p["closed"] = True
        p["closed_at"] = _now()
        try:
            await store.save_poll(p.get("poll_id"), p)
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't close the poll: {exc}")
            return
        LOG.info("Poll %s closed by %s", p.get("poll_id"), ctx.author)
        await ctx.send(f"🔒 **{p.get('question')}** is closed. Final results: "
                       f"`/poll results {p.get('poll_id')}`")

    # ── persistence ────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_ready(self):
        """Re-attach vote buttons after a restart.

        Button interactions are only delivered for views the bot knows about;
        polling every open poll and registering its view keeps buttons on old
        messages alive across restarts and cog reloads.
        """
        if self._views_registered:
            return
        self._views_registered = True
        try:
            polls = await store.list_polls()
        except StoreError:
            return
        for poll in polls:
            if poll.get("closed"):
                continue
            self.bot.add_view(PollVoteView(self, poll))


async def setup(bot):
    await bot.add_cog(Polls(bot))