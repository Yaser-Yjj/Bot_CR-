# 🤖 **Bot_CR** 🤖

**Bot_CR** is the Robotics Club's Discord bot: onboarding, engagement, stats,
moderation and levelling, all backed by an **Appwrite** data store. Every
command works as both `!prefix` and `/slash`.

---

## ✨ Features

- 🪪 **Real-name onboarding** — new members get a temp role, then a DM button
  opens a modal to enter their **real full name**. The bot applies a
  title-cased *cursive* nickname (`𝓨𝓪𝓼𝓼𝓮𝓻 𝓔𝓵 𝓙𝓸𝓾𝓷𝓭𝓲`), grants the verified +
  member roles, and asks for their birthday. No more reaction-based
  verification (anyone could have verified or kicked others) — now the member
  proves it with their real identity.
- 🎂 **Birthday tracker** — birthdays are stored per member in Appwrite and
  announced in the announcements channel on the day (plus an immediate
  announcement if you set a birthday that *is* today).
- 📈 **XP & levels** — chatting earns XP (per-user cooldown), levels grow with
  `100·L²` cumulative XP, level-ups are shouted in chat, and `/rank` +
  `/leaderboard` read straight from the store.
- 🔥 **Daily challenges** — staff set a challenge for the day
  (`/challenge set`), members claim it once (`/challenge claim`), history is
  kept, and each day's challenge is auto-posted to announcements.
- 📊 **Live dashboard** — member/message/voice counters persisted in Appwrite,
  refreshed every `DASHBOARD_REFRESH_SECONDS` (default 60 s). No more
  full-channel history scans.
- 🛡️ **Moderation** — `/kick`, `/ban`, `/unban`, `/timeout`, `/untimeout`,
  `/mute`, `/unmute`, `/warn`, `/modlog`, and `/del`. Every action is recorded
  in the Appwrite modlog with the moderator, target, reason and timestamp.
- 🔑 **Bot-staff override** — the Archon, bot-developer and bot-admin roles
  (config `ROLE_ARCHON` / `ROLE_BOT_DEVELOPER` / `ROLE_BOT_ADMIN`) can run
  every staff command without needing the guild permissions, exactly like the
  server owner did before.
- 🎖️ **Role management** — `/addrole <role> <@members...>` and
  `/removerole <role> <@members...>` assign/remove roles on the spot;
  `/setlead <role> <@members...>` **replaces** the holders of a leadership
  role (Lead / Vice President / President from `LEADER_ROLES`) with exactly
  the members you tag — previous holders are stripped first.
- 🌐 **Public-API commands** — `/weather`, `/define`, `/meme`, `/crypto`,
  `/spacex`, `/github`, `/advice` and `/lyrics`, all keyless, selected from
  openpublicapis.com.
- 🎵 **Music** — `/play <song>` streams YouTube audio (YTMusic search + yt-dlp
  + ffmpeg) into your voice channel: `/pause`, `/resume`, `/skip` (majority
  vote — requester and staff skip instantly), `/stop`, `/loop`, `/volume`,
  `/queue` (plus `/queue auto` to generate a 📻 YTMusic radio queue from the
  current track), `/nowplaying`, an interactive panel with pause / vote-skip /
  loop / stop / lyrics buttons, and auto-disconnect after a minute of idle.
- 🤖 **Private meeting rooms** — `/meeting create @a @b [name]` spins up a
  private VC (auto-deleted when empty), `/meeting end` cleans it up.
- 🔐 **Club permission scopes** — an authorization layer instead of scattered
  role checks: every club command is gated on a scope (`tasks.create`,
  `members.manage`, `competitions.manage`, …) resolved from the member's
  **linked club account** (Discord → club account → club role → cell) with
  Discord-side staff roles and server admins bootstrapping full access.
- 👤 **Member system** — `/link <club-id> [real name]` ties your Discord to
  your club account (role/cell are set by leadership via `/setprofile`, so you
  can never self-promote), `/profile`, `/whois @member` (staff),
  `/roles` (what your access means), `/hierarchy`, and a permission-aware
  `/dashboard` with quick-action buttons.
- 🧩 **Cells** — `/cell add <@members...> [cell]` places members into a cell:
  Cell Chiefs can pull people into their **own** cell, while leadership and bot
  staff may name any cell. A plain Core Member is promoted to Cell Member
  (higher ranks are never demoted) and moving a member out of another cell is
  reported back.
- 📋 **Tasks** — `/task create/assign/claim/complete/edit/cancel` +
  `/tasks` / `/tasks overdue` with priority colours and due dates; creation
  and assignment are permission-gated, and everything lives in Appwrite so the
  app sees the same tasks.
- 🏆 **Competitions** — `/competitions` lists upcoming events,
  `/competition <name>` shows details with capacity-enforced **Register /
  Unregister** buttons that write back to Appwrite.
- 📅 **Events & attendance** — `/event create` (chiefs+), `/events`, and
  `/event <title>` with ✅/❌/❔ RSVP buttons whose answers land in Appwrite —
  one source of truth for attendance.
- 🗳️ **Polls & votes** — `/poll create` (transparent or anonymous,
  single/multiple choice) posts a panel with **one button per option**: tap to
  vote and the count goes up **live on the message**. Transparent polls show
  the per-option breakdown and who voted for what ("who's coming to the
  competition?"); anonymous polls hash the voter and show only the aggregate
  total on the panel, so anonymity hides both *who* voted and *how*, until
  `/poll close` reveals the final counts. Single-choice polls confirm when a
  tap **changes** your existing vote, and an optional locked-results mode hides
  everything until close. Every vote carries a timestamp and polls record
  `created_at` / `closed_at`, all persisted in Appwrite for the dashboard.
- 🔔 **Notifications** — `/notifications` toggles task/event/competition/
  announcement preferences (stored per member).
- 🔬 **Robotics fun** — `/robot` telemetry readout and a multiple-choice
  `/quiz` (engineering + robotics questions).
- 🛡️ **Channel controls** — `/slowmode`, `/lock`, `/unlock`.
- 👋 **Welcome & goodbye**, 📜 **rules board**, and 🎲 **fun commands**
  (8ball, coinflip, dice, slap, hug, joke, fact, compliment, rps, ship,
  choose, reverse, clap, roast, quote, website).

---

## ⚡ Commands

All commands are hybrid (prefix **and** slash). `/help` / `!help` opens the
interactive menu — **one button per section** (General, Fun, Entertainment,
Cell Management, Events & Meetings, Competitions, Polls, Members & Stats,
Moderation & Rules, Server Ops), with General front and centre.

| Command | Who | What it does |
|---|---|---|
| `help` | everyone | Interactive help with section buttons, grouped by topic |
| `hello`, `ping` | everyone | Hello / latency check |
| `rank [member]` | everyone | Level, XP and progress bar |
| `leaderboard` | everyone | Top 10 by XP |
| `challenge today` | everyone | Today's challenge + claims |
| `challenge claim` | everyone | Claim today's challenge (once) |
| `challenge history` | everyone | The last 7 challenges |
| `challenge set <title> [desc]` | staff | Set today's challenge |
| `total_messages` | everyone | Total server messages (persisted) |
| `total_voice_time` | everyone | Total voice time (persisted) |
| `current_voice_time` | everyone | Time in your voice channel this session |
| `online_members` | everyone | Online/idle/dnd member count |
| `8ball`, `coinflip`, `dice`, `joke`, `fact`, `compliment` | everyone | Fun 🎲 |
| `slap <member>`, `hug <member>` | everyone | Social fun |
| `del <n>` | staff | Bulk-delete up to 100 messages |
| `warn <member> [reason]` | staff | Record a warning + modlog entry |
| `timeout <member> <min> [reason]`, `untimeout` | staff | Timeouts |
| `mute <member> [min]`, `unmute` | staff | Mute via Discord timeout |
| `kick <member> [reason]` | staff | Kick |
| `ban <member> [reason]`, `unban <id>` | staff | Ban / unban |
| `fixname <member>` | staff | Re-apply cursive nickname |
| `modlog [limit]` | staff | Recent moderation actions |
| `addrole <role> <@members...>` | staff | Give a role |
| `removerole <role> <@members...>` | staff | Take a role away |
| `setlead <role> <@members...>` | staff | Replace leadership holders (Lead/VP/President) |
| `weather <city>`, `define <word>`, `meme` | everyone | Open-Meteo / dictionary / meme |
| `crypto [coin]`, `spacex`, `github <user>`, `advice` | everyone | Live data APIs |
| `lyrics [song]` | everyone | LRCLIB lyrics — omit the song to look up the current track |
| `play <song>` | everyone | Stream music (joins your voice channel) |
| `queue [auto]` | everyone | Show the queue, or generate a radio queue from the current track |
| `pause`, `resume` | everyone | Pause / resume music |
| `skip` | everyone | Vote to skip (requester/staff: instant) |
| `stop`, `loop`, `volume <1-100>`, `nowplaying` | everyone | Music control |
| `meeting create <@members...> [name]`, `meeting end` | everyone | Private VC room |
| `link <club-id> [real-name]`, `unlink` | everyone | Link/unlink your club account |
| `profile [member]` | everyone | Identity hub: Overview · Minecraft link · Robotics (soon) tabs |
| `roles` | everyone | What your club role can do (scopes) |
| `hierarchy` | everyone | Club org chart |
| `dashboard` | everyone | Permission-aware club overview |
| `whois <member>` | staff | Internal record (club ID, warnings, prefs) |
| `setprofile <member> role/cell/club-id` | leadership | Set club role / cell / club ID |
| `cell add <@members...> [cell]` | chiefs+ | Add members to a cell (chiefs → their own; staff pick any) |
| `notifications` | everyone | Toggle notification categories |
| `tasks` / `tasks mine` | everyone | Your open tasks, colour-coded |
| `tasks overdue` | everyone | Overdue tasks (staff: whole club) |
| `task view <id>` | everyone | Full task detail + complete/claim buttons |
| `task create <title> [priority] [due] [cell]` | chiefs+ | Create a task |
| `task assign <id> @member` | chiefs+ | Assign a task |
| `task claim <id>` | cell members | Claim an unassigned task |
| `task complete <id>` | assignee | Mark your task done |
| `task edit <id> [title] [priority] [due] [status] [cell]` | chiefs+ | Edit a task |
| `task cancel <id>` | chiefs+ | Cancel a task |
| `competitions` | everyone | Upcoming competitions |
| `competition <name>` | everyone | Details + Register/Unregister buttons |
| `competition create <name> [date] [location] [capacity]` | leadership | Add a competition |
| `events` | everyone | Upcoming events |
| `event <title>` | everyone | Details + ✅/❌/❔ RSVP buttons |
| `event create <title> [date] [time] [location]` | chiefs+ | Schedule an event |
| `poll` / `poll list` | everyone | Overview of every poll |
| `poll create <question> <options> [mode] [selection] [hide_results]` | everyone | Create a poll (`\|`-separated options; transparent or anonymous, single or multiple) with one vote button per option |
| `poll vote <id> <option>` | everyone | Vote by command (buttons on the poll are the quick way; same again = undo) |
| `poll results <id>` | everyone | Live results — voters named for transparent, counts only for anonymous |
| `poll close <id>` | staff / creator | Stop voting and stamp `closed_at` |
| `robot` | everyone | Playful telemetry readout |
| `quiz` | everyone | 5-question robotics quiz |
| `slowmode <seconds>`, `lock`, `unlock` | staff | Channel controls |

> 💡 **Poll examples**
> - Attendance (transparent — everyone sees the names):
>   `/poll create "Who's coming to RoboCup?" "Yes|No|Maybe"`
> - Secret Chief ballot (anonymous — counts only, and hidden until close):
>   `/poll create "Next Chief of IT?" "SwirX|Yaser|Taybi" anonymous single hide_results`
> - **Vote in one tap**: every poll has a button per option — click it and the
>   counts update in place. `/poll vote P-1 2` does the same thing by command
>   (pick the same thing again to undo). Results are always live:
>   `/poll results P-1`.

---

## 🗄️ Data layer

All persistent state lives in the club's Appwrite project — no JSON files, no
in-memory counters that vanish on restart:

| Collection | Contents |
|---|---|
| `bot_members` | one doc per member (doc id = Discord user id): real name, cursive nickname, birthday, joined date, verified flag, XP, messages, voice seconds, warnings, **linked club account** (`club_id`, `club_role`, `cell`), **identity links** (`links.minecraft` username/type/UUIDs/date, `links.robotics` reserved), `lang` (menu language), notification prefs |
| `bot_counters` | global totals (messages, voice seconds) flushed incrementally |
| `bot_challenges` | one doc per date: title, description, who claimed it |
| `bot_modlog` | every moderation action with moderator/target/reason/timestamp |
| `bot_settings` | generic key → value storage |
| `bot_tasks` | tasks: id (`T-1`), title, description, assignee, cell, status, priority, due date, created by/at |
| `bot_competitions` | competitions: name, date, location, capacity, registered (user-id array) |
| `bot_events` | events: title, date/time/location, description, attendees + declined arrays |
| `bot_polls` | polls: id (`P-1`), question, options (array), mode (transparent/anonymous), selection (single/multiple), hide_results, closed/closed_at, created_by/created_at, votes (JSON per vote: voter id/hash, option index, timestamp) |

The bot connects with a server-side API key (no user auth), and the schema
(collections, attributes, indexes) is provisioned **idempotently on boot** or
via the standalone script:

```bash
python scripts/bootstrap_appwrite.py
```

High-frequency events (messages, voice time, XP) are accumulated in memory and
flushed in small batches to keep writes in the single-digits-per-minute range.

---

## 🛠️ Setup

1. **Python 3.10+** (Python 3.13/3.14 pull `audioop-lts` automatically via
   requirements).
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create your environment file and fill it in:
   ```bash
   cp .env.example .env
   ```
   Required: `BOT_TOKEN`, `APPWRITE_ENDPOINT`, `APPWRITE_PROJECT_ID`,
   `APPWRITE_API_KEY`, `APPWRITE_DATABASE_ID` (channel/role names and behaviour
   knobs all default in `.env.example`).
4. Provision the Appwrite schema (idempotent, safe to re-run):
   ```bash
   python scripts/bootstrap_appwrite.py
   ```
5. Start the bot:
   ```bash
   python BOT.py
   ```

> 💡 Set `GUILD_ID` in `.env` during development so slash commands sync
> instantly to one server. Leave it empty to sync globally (up to an hour).
> Channel/role names are all configurable via env — no code changes needed if
> your server renames things.

### 🍪 YouTube cookies for `/play` (recommended)

YouTube bot-flags datacenter-server IPs, which makes most `yt-dlp` extractions
fail with *"Sign in to confirm you're not a bot"*. **YTMusic search still
works** (the bot finds the right song every time), but the actual audio stream
needs a trusted session. Fix it once with cookies:

1. In a browser you're logged into YouTube with, install a cookies-exporter
   extension (e.g. *Get cookies.txt LOCALLY*) and export cookies for
   `youtube.com` as a Netscape-format `cookies.txt`.
2. Place it on the server (e.g. `/home/ubuntu/Bot_CR/cookies.txt`).
3. Point the bot at it in `.env`:
   `YT_COOKIES_FILE=/home/ubuntu/Bot_CR/cookies.txt`
4. Restart the bot. `/play` now streams reliably, and `/queue auto` radio
   works too.

> 🔒 Treat `cookies.txt` like a password — it grants YouTube access as that
> account. Keep it out of version control and out of the repo.

---

## ⛏️ Minecraft server bridge (`/mc`, `/minecraft`, `/linkmc`)

The club's Minecraft server (Robotics CMC) is managed through **Pterodactyl's
Client API** — no Minecraft plugins required:

- `/mc` or `/minecraft` — opens the **interactive Minecraft hub**: the join
  address `minecraft.alibks.dev:25566`, the server version (from a standard
  server-list ping), run state, player count and your own Discord ↔ Minecraft
  link status, with **Dank-Memer-style button drill-downs**:
  - 🔄 **Refresh** — re-check the live status;
  - 🔗 **Link your account** → 💳 **Paid** / 🆓 **Free** → a modal asks for your
    exact Minecraft username → whitelisted and saved;
  - ❌ **Unlink** → confirmation → removes the whitelist entries and the
    Discord ↔ Minecraft link;
  - 🎛 **Server control** (visible **only** to the bot operator / Archon):
    ▶️ Start · ⏹ **Stop** · 🔄 **Restart** — same power signals as
    `/mcstart` `/mcstop` `/mcrestart`.
- `/linkmc <username> <free|paid>` — the same linking as a **one-liner slash
  command**; the member **declares their account type**:
  - **paid** (bought Minecraft, e.g. `SwirXwasTaken`) → whitelists the **real
    UUID** *and* the offline UUID, so they're covered whether they join with
    the official launcher or a free one (the server runs `online-mode=false`).
  - **free** (cracked/offline account) → whitelists the **offline UUID of the
    exact name as typed** (case-sensitive — Mojang's capitalisation of the
    same letters is a different account, e.g. `hatim` vs `Hatim`).
  In both cases `whitelist.json` is written through the panel file API and
  live-reloaded (`whitelist reload`) when the server is running, or left to
  apply at next start when it's stopped.
  The Discord ↔ Minecraft link is stored per member (`links.minecraft`, see the
  identity model below) so staff can audit it and the profile hub can show it.

### 🔗 Identity links (`/profile`)

Every member doc carries a structured `links` object — a place per platform
that gets linked to the Discord account:

```jsonc
"links": {
  "minecraft": { "username": "SwirXwasTaken", "type": "paid",
                 "uuids": ["real-uuid", "offline-uuid"], "linked_at": "…" },
  "robotics":  null   // reserved — coming soon
}
```

`/profile` is now an **identity hub** with tab buttons (Overview · ⛏️ Minecraft
· 🔬 Robotics): the Minecraft tab shows the link card (username / type /
whitelisted UUIDs / date) with quick **Link / Unlink** actions. Link/unlink
from the *hub* or the *profile* are the same flows, and only the member
themselves (or the operator/Archon) can link or unlink a profile — a plain
viewer sees the card without the action buttons and without the UUIDs. Adding
the robotics link later is just filling in `links.robotics`.

Configure it in `.env`:

```
MC_PTERO_URL=https://panel.minecraft.bouyakhsass.com
MC_PTERO_CLIENT_KEY=<client API key from Account → API Credentials>
MC_SERVER_ID=<server identifier, e.g. 96f52139>
MC_ADDRESS=minecraft.alibks.dev
MC_PORT=25566
MC_SERVER_NAME=Robotics CMC
# Optional: who may run /mcstart /mcstop /mcrestart (defaults to BOT_ADMIN_USER_IDS).
# MC_CONTROL_USER_IDS=407922956757499905
```

> 🔒 The client API key can manage the panel's servers, so keep it out of
> version control — it lives in `.env` only.

### 🎛 Server power control (`/mcstart`, `/mcstop`, `/mcrestart`)

Only the **bot operator** and the **Archon** role can start/stop/restart the
server — deliberately *not* pres/VP, other staff roles, or server admins:

- `/mcstart` — boot the server.
- `/mcstop` — graceful shutdown.
- `/mcrestart` — graceful restart (offline server → hints `/mcstart` instead).

The operator's IDs come from `MC_CONTROL_USER_IDS` (defaults to
`BOT_ADMIN_USER_IDS`). The Archon role is matched by `ROLE_ARCHON` (fallback:
any role whose name contains "archon", so emoji-prefixed names survive).
They send Pterodactyl power signals through the **Client API** (`/power`), so
the panel key needs power scope on top of file/console.

### 🏷 Player tagging & join rally

Every member who links a Minecraft account is auto-assigned the
**`MC_PLAYER_ROLE`** role (created by the bot on first use, backfilled for
existing links on boot) and loses it again on unlink — so **one `@role`
mention pings the whole linked player base**:

- **`/mcsession [message]`** — MC operators (`MC_CONTROL_USER_IDS`) or anyone
  with the `announcements.create` scope (VP+ / bot staff) posts an
  **English rally card** in the announcements channel with a live join address.
  The ack message itself is localized per member.
- **Auto join-rally** — the bot streams the server console over a
  **Pterodactyl websocket** (no plugins) and watches for `joined the game`.
  Joins are burst-coalesced, then capped at 5 names (+N) and posted as one
  role ping to the announcements channel, **at most once per
  `MC_RALLY_COOLDOWN`** (default 2700 s). The live names also enrich the
  “Players” line on the `/mc` hub while the watcher is connected.

Additional env vars:

```
MC_PLAYER_ROLE=⛏️ Minecraft Player      # role auto-assigned on link
MC_RALLY_COOLDOWN=2700                  # min seconds between auto pings
```

### 🔐 mc-link (Discord ↔ Minecraft single sign-on, `/mclink`, `/mcpass`)

The cracked (offline-mode) server can't trust what clients claim, so a Paper
plugin gates logins through **AuthMe** and this bot — the **only** component
that mints links and credentials — anchors every name to a Discord identity.
Minecraft is an **untrusted client boundary**: a username, UUID or "op" flag a
client presents is data, never proof. The Appwrite backend (`mc_link_codes`,
`mc_auth`, `mc_challenges`) is the shared source of truth between the bot and
the plugin; the plugin only *consumes* backend state and bot-issued secrets.

- **`/mclink <username>`** (hybrid, guild-only, 2/60 s cooldown) — validates
  with the existing `_USERNAME_RE`, refuses names already linked to a *different*
  Discord account, then mints a single-use 6-char code in `mc_link_codes`
  (TTL `MC_LINK_CODE_TTL`) and **DMs it** — codes and temp passwords are never
  posted in a channel. In-game `/mcverify <code>` claims it.
- **Watchers (~`MC_LINK_POLL_SECONDS`)** — `status=used` codes get a thanks DM,
  the `MC_PLAYER_ROLE` role, a `bot_members.links.minecraft` sync (`type=linked`,
  shown on `/mc` and `/profile`), and an audit entry. `mc_challenges`:
  `new_ip` pending → a **12-char temp password**, AES-256-GCM sealed to
  `payload_enc` (AAD = username), `approved`, DM'd with a **Deny** path;
  `change_password` `done`/`failed` → DM confirmation/soft-failure.
  Markers in `bot_settings` resume after restarts (at-least-once delivery).
- **`/mcpass`** (slash) and the **🔑 Change password** button on `/mc` and the
  Minecraft tab of `/profile` share one modal (8–64 chars + confirm) → an
  encrypted `change_password` challenge the plugin applies server-side.
- **📱 Devices** button (linked accounts only) — lists `current_ip` + `last_ips`
  from `mc_auth`, IPs **masked by default** (`203.0.113.***`, raw only on a
  detail tap), each removable behind a confirm (owner/operator only). Removal
  edits `mc_auth.last_ips` directly so that IP triggers a fresh new-IP
  challenge instead of auto-login — the café / school-Wi-Fi case.

```
MC_LINK_SECRET=...                     # AES-256-GCM key, 32 bytes hex, shared with the plugin
MC_LINK_CODE_TTL=300                   # link-code lifetime (s)
MC_TEMP_TTL=300                        # temp-password lifetime (s)
MC_LINK_POLL_SECONDS=5                 # watcher poll interval (s)
```

The crypto twin (`cogs/_mc_crypto.py`) must stay byte-compatible with the
plugin's `CipherBox` (`iv + tag + ciphertext`, hex, AAD = username); crypto and
credential-mint helpers are covered by `scripts/test_mclink.py` (run alongside
the smoke test; the watcher loops are guarded so an empty `MC_LINK_SECRET`
disables the whole cog instead of crashing).

### 🔄 `/bot status` update checker

`/bot status` now compares the running build to **`origin/nightly`** (the same
ref `/bot update` pulls) — fetch is cached 120 s — and shows:

- ✅ **Up to date** when `HEAD` equals (or is ahead of) the remote;
- ⬆️ **N commits behind** with a 3-commit preview and an **`⬆️ Update to
  nightly (N commits)` button** (bot-admin interaction only);
- a **confirm-first** step that warns it runs `git pull --ff-only origin nightly`
  and restarts the bot — identical to `/bot update`, restart-ack included;
- ⚠️ a **diverged** note when fast-forwarding isn't possible (manual redeploy).

---

## 🌐 Languages (`/settings`, `/language`)

The bot speaks **English, French and Arabic**. Which language you see depends
on *who triggered the command*:

- `/settings` opens an interactive menu (Dank Memer style): the message's
  content is the current state, and **buttons drill down until the choice** —
  `⚙️ Settings → 🌐 Language → 🇬🇧 English / 🇫🇷 Français / 🇸🇦 العربية`.
- `/language <english|french|arabic>` is the quick, non-interactive version.
  Both work in a server **or in a DM** (private message to the bot).
- Resolution per member: their stored `lang` → their **Discord locale** (if it
  matches a supported language) → English.
- **Server-broadcast messages** (welcome channel, dashboard tracker,
  announcements) stay in **English** — the server's default language — and are
  unaffected by member choices.

Strings live in `i18n/{en,fr,ar}.json` (one table per language, flat keys with
`{placeholder}` formatting). `i18n/core.py` holds the `t(key, lang)` lookup and
the resolver; new cogs import `resolve_member_lang(ctx)` + `t()`.

---

## 🚀 Deploy (Render)

`render.yaml` describes a free-worker service. `BOT_TOKEN` and
`APPWRITE_API_KEY` are marked `sync: false` — set them in the Render dashboard
(never in the repo). `KeepAlive.py` runs a tiny Flask server on `$PORT` so a
ping service can keep the free instance awake.

---

## 🧱 Project structure

```
BOT.py                    launcher — logging, store init, cog discovery, tree sync
config.py                 every knob is an env var
data/                     Appwrite connectivity + async store
  appwrite_client.py      schema (collections/attributes/indexes) + bootstrap
  store.py                async typed wrappers (members, counters, challenges, modlog, settings, tasks, competitions, events, polls)
cogs/                     one file per feature; auto-discovered
  onboarding.py           join → name modal → cursive nickname → roles → birthday
  birthday_tracker.py     daily + immediate birthday announcements (store-backed)
  stats.py                messages / voice / presence tracking + periodic flush
  dashboard.py            persisted-counter dashboard embed
  engagement.py           XP & levels, daily challenges
  fun.py                  lighthearted commands (+ robot status, robotics quiz)
  moderation.py           kick/ban/timeout/warn + modlog + lock/slowmode/unlock
  welcome.py / goodbye.py join / leave messages
  rules.py                rules board
  general.py              hello / ping / custom help
  _perms.py / _scopes.py  staff bypass + club permission-scope resolver
  members.py              link/unlink, profiles, hierarchy, notifications, dashboard
  cells.py                /cell add — place members into a cell
  tasks.py                task CRUD + lists (priority/due/cell)
  competitions.py         competitions + registration
  events.py               events + RSVP attendance
  polls.py                polls + votes (transparent / anonymous, Appwrite-backed)
  _dates.py               shared date parsing/formatting helpers
scripts/
  bootstrap_appwrite.py   idempotent schema provisioning
  smoke_test.py           hermetic cog-load check (used by CI)
KeepAlive.py              Flask keep-alive on $PORT
render.yaml               Render worker service definition
```

**Adding a feature = dropping a new file in `cogs/`.** The launcher discovers
it automatically.

---

## ✅ Quality gates

`.github/workflows/ci.yml` runs on every push/PR against Python 3.12 and 3.13:
byte-compiles every module, runs `scripts/smoke_test.py` (loads all cogs,
asserts every command is hybrid and the custom `help` is installed) and the
hermetic `scripts/test_mclink.py` (CipherBox interop vectors + credential mint).

```bash
python scripts/smoke_test.py   # run the same check locally
python scripts/test_mclink.py  # mc-link crypto/credential unit checks
```

---

## 🌟 Contributors 🌟

A huge thanks to the amazing folks who helped bring **Bot_CR** to life! 👏✨

- **[Hamza](https://github.com/Yasahiru)** - The genius behind it all! 🤓💡
- **[Kawtar](https://github.com/ELGADDIxKawtar)** - For her outstanding contributions! 🧑‍💻🌟
- **[Wieam](https://github.com/wieam-ar)** - For her dedication and hard work! 🧑‍💻💪
- **[Yaser](https://github.com/0yaser0)** - For his innovative ideas and passion! 🧑‍💻🔥

---

## 📝 License 📝

This project is open-source and licensed under the MIT License.
Feel free to fork it, contribute, and make it your own! 🔄📜