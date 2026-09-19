"""Club task management — Appwrite-backed, permission-scoped.

``/task`` CRUD for chiefs/staff, ``/tasks`` for members. Tasks carry priority,
due dates, status and a cell; everything lives in the bot_tasks collection so
the app and the bot share one source of truth.
"""

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from data.store import store
from data.store import StoreError
from cogs._dates import days_until, fmt_date, is_overdue, parse_due
from cogs._scopes import require_scope, scopes_for_author
from cogs._ui import PaginatorView

LOG = logging.getLogger("bot.tasks")

PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}
STATUS_EMOJI = {"open": "🚩", "in_progress": "🔧", "done": "✅", "cancelled": "🚫"}

PRIORITY_ALIASES = {
    "high": "high", "urgent": "high", "critical": "high", "🔥": "high", "red": "high",
    "medium": "medium", "normal": "medium", "default": "medium", "mid": "medium",
    "low": "low", "minor": "low", "nice": "low", "later": "low",
}

STATUS_ALIASES = {
    "open": "open", "new": "open", "todo": "open",
    "in_progress": "in_progress", "in progress": "in_progress", "doing": "in_progress",
    "wip": "in_progress", "progress": "in_progress",
    "done": "done", "complete": "done", "completed": "done", "finished": "done",
    "cancel": "cancelled", "cancelled": "cancelled", "canceled": "cancelled",
    "closed": "cancelled",
}


def _norm_priority(text) -> str:
    return PRIORITY_ALIASES.get(str(text or "").strip().lower(), "medium")


def _norm_status(text) -> str:
    key = str(text or "").strip().lower()
    return STATUS_ALIASES.get(key, key)


def _task_embed(task: dict, member_name: str = "") -> discord.Embed:
    color = {"high": discord.Color.red(), "medium": discord.Color.gold(),
             "low": discord.Color.green()}.get(str(task.get("priority")), discord.Color.gold())
    embed = discord.Embed(
        title=f"{PRIORITY_EMOJI.get(task.get('priority'), '🟡')} {task.get('title', '?')}",
        description=task.get("description") or "",
        color=color,
    )
    embed.add_field(name="Status",
                    value=f"{STATUS_EMOJI.get(task.get('status'), '🚩')} {task.get('status')}",
                    inline=True)
    due = task.get("due") or "—"
    if task.get("due") and is_overdue(task.get("due")) and not task.get("status") in ("done", "cancelled"):
        due += " ⚠️"
    embed.add_field(name="Due", value=due, inline=True)
    embed.add_field(name="Cell", value=task.get("cell") or "—", inline=True)
    embed.add_field(name="Assignee",
                    value=f"<@{task['assignee']}>" if task.get("assignee") else "—",
                    inline=True)
    embed.add_field(name="Created by",
                    value=f"<@{task['created_by']}>" if task.get("created_by") else "—",
                    inline=True)
    embed.set_footer(text=f"Task {task.get('task_id')}")
    return embed


class TaskDetailView(discord.ui.View):
    """Quick actions on a task: complete it or claim it."""

    def __init__(self, cog, task_id: str, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.task_id = task_id
        self.user_id = user_id

    @discord.ui.button(label="✅ Complete", style=discord.ButtonStyle.success,
                       custom_id="task:complete")
    async def complete(self, interaction, button):
        task = await self.cog._require_decided(interaction, self.task_id)
        if task is None:
            return
        if not (str(task.get("assignee")) == str(interaction.user.id)
                or await self.cog._staff_ok(interaction)):
            await interaction.response.send_message(
                "🔒 Only the assignee (or staff) can complete this task.",
                ephemeral=True)
            return
        task["status"] = "done"
        task["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.save_task(task)
        await interaction.response.edit_message(
            embed=_task_embed(task), view=self)

    @discord.ui.button(label="🙋 Claim", style=discord.ButtonStyle.secondary,
                       custom_id="task:claim")
    async def claim(self, interaction, button):
        task = await self.cog._require_decided(interaction, self.task_id)
        if task is None:
            return
        if task.get("assignee"):
            await interaction.response.send_message(
                "🚩 This task already has an assignee — use another task.",
                ephemeral=True)
            return
        task["assignee"] = str(interaction.user.id)
        task["status"] = "in_progress"
        task["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.save_task(task)
        await interaction.response.edit_message(embed=_task_embed(task), view=self)


class Tasks(commands.Cog):
    """Create, assign and track club tasks."""

    def __init__(self, bot):
        self.bot = bot

    # ── helpers ─────────────────────────────────────────────────
    async def _find_task(self, indicator: str) -> dict | None:
        """Find by task id (T-5) or first title match."""
        indicator = indicator.strip()
        try:
            tasks = await store.list_tasks()
        except StoreError:
            return None
        for task in tasks:
            if str(task.get("task_id")) == indicator:
                return task
        lower = indicator.lower()
        for task in tasks:
            if indicator.lower() in str(task.get("title", "")).lower():
                return task
        return None

    def _visible_tasks(self, tasks, member_id: int, is_staff: bool):
        """Staff see everything; members see their own tasks."""
        if is_staff:
            return tasks
        return [t for t in tasks if str(t.get("assignee")) == str(member_id)]

    async def _staff_ok(self, ctx_or_interaction) -> bool:
        scopes = await scopes_for_author_like(ctx_or_interaction)
        return "tasks.assign" in scopes

    async def _require_decided(self, interaction, task_id: str):
        task = await self._find_task(task_id)
        if task is None:
            await interaction.response.send_message(
                "⚠️ Task not found (it may have been deleted).", ephemeral=True)
            return None
        return task

    # ── /tasks ─────────────────────────────────────────────────
    @commands.hybrid_group(name="tasks", description="List your tasks.",
                           invoke_without_command=True, fallback="mine")
    @commands.guild_only()
    @require_scope("tasks.read")
    async def tasks(self, ctx):
        """You (or staff) -> a colour-coded list of open/in-progress tasks."""
        await self._send_task_list(ctx, member=ctx.author, all_staff=False)

    @tasks.command(name="overdue", description="Tasks past their due date.")
    @commands.guild_only()
    @require_scope("tasks.read")
    async def tasks_overdue(self, ctx):
        """Overdue tasks — staff see the whole club, everyone else their own."""
        await self._send_task_list(ctx, member=ctx.author, all_staff=False,
                                   overdue_only=True)

    async def _send_task_list(self, ctx, *, member, all_staff, overdue_only=False,
                              status_filter=None):
        scopes = await scopes_for_author(ctx)
        is_staff = "tasks.assign" in scopes
        if all_staff or is_staff:
            tasks = await self._safe_list()
        else:
            tasks = [t for t in await self._safe_list()
                     if str(t.get("assignee")) == str(member.id)]
        if overdue_only:
            tasks = [t for t in tasks if is_overdue(t.get("due"))
                     and t.get("status") not in ("done", "cancelled")]
        if status_filter:
            tasks = [t for t in tasks if t.get("status") == status_filter]
        tasks.sort(key=lambda t: t.get("due") or "9999")
        if not tasks:
            await ctx.send("🎉 No tasks" + (" overdue" if overdue_only else "") + "! 🎉")
            return
        lines = []
        for t in tasks:
            emoji = PRIORITY_EMOJI.get(t.get("priority"), "🟡")
            due = fmt_date(t.get("due"))
            suffix = " ⚠️" if is_overdue(t.get("due")) and t.get("status") not in ("done", "cancelled") else ""
            status = f"[{STATUS_EMOJI.get(t.get('status'), '🚩')} {t.get('status')}]"
            lines.append(f"{emoji} **{t.get('title')}** · {due}{suffix}\n"
                         f"   `{t.get('task_id')}` {status}")
        title = "⏰ Overdue tasks" if overdue_only else "📋 Your tasks"
        hint = "Tips: /task view <id>, complete, claim · /task create (staff)"
        page_size = 10
        pages = [f"{title}\n\n" + "\n\n".join(lines[i:i + page_size]) + f"\n\n{hint}"
                 for i in range(0, len(lines), page_size)]
        if len(pages) == 1:
            await ctx.send(pages[0])
        else:
            await ctx.send(pages[0], view=PaginatorView(pages))

    async def _safe_list(self):
        try:
            return await store.list_tasks()
        except StoreError as exc:
            LOG.warning("tasks list failed: %s", exc)
            return []

    # ── /task ──────────────────────────────────────────────────
    @commands.hybrid_group(name="task", description="Task operations.",
                           invoke_without_command=True)
    @commands.guild_only()
    async def task(self, ctx):
        """Usage: /task view|create|assign|complete|claim|edit|cancel."""
        await ctx.send("📋 **/task** usage\n"
                       "`view <id>` · `create <title> [priority] [due] [cell]`\n"
                       "`assign <id> @member` · `complete <id>` · `claim <id>`\n"
                       "`edit <id> [...]` · `cancel <id>`\n"
                       "List: `/tasks` or `/tasks overdue`.")

    @task.command(name="view", description="Show a task's full details.")
    @commands.guild_only()
    @require_scope("tasks.read")
    async def task_view(self, ctx, task: str):
        task_doc = await self._find_task(task)
        if task_doc is None:
            await ctx.send(f"⚠️ No task matching **{task}**.")
            return
        scopes = await scopes_for_author(ctx)
        if not ("tasks.assign" in scopes or
                str(task_doc.get("assignee")) == str(ctx.author.id)):
            await ctx.send("🔒 That task isn't yours to view.")
            return
        await ctx.send(embed=_task_embed(task_doc),
                       view=TaskDetailView(self, str(task_doc["task_id"]),
                                           ctx.author.id))

    @task.command(name="create", description="Create a task (staff/chiefs).")
    @commands.guild_only()
    @require_scope("tasks.create")
    async def task_create(self, ctx, title: str, description: str = "",
                          priority: str = "medium", due: str = "",
                          cell: str = ""):
        """Create an unassigned task; assign it next with /task assign."""
        if len(title) > 255:
            await ctx.send("⚠️ Keep the title under 255 characters.")
            return
        try:
            due_norm = parse_due(due) if due else ""
        except ValueError:
            await ctx.send("⚠️ Couldn't parse due date — use YYYY-MM-DD, 'tomorrow' or 'in 3d'.")
            return
        code = await store.next_task_code()
        task_doc = {
            "task_id": code,
            "title": title.strip(),
            "description": description.strip(),
            "priority": _norm_priority(priority),
            "due": due_norm,
            "cell": cell.strip(),
            "status": "open",
            "created_by": str(ctx.author.id),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await store.save_task(task_doc)
        except StoreError as exc:
            await ctx.send(f"⚠️ Couldn't create the task: {exc}")
            return
        await ctx.send(f"📋 Created **{title.strip()}** as `{code}`.\n"
                       f"Assign it: `/task assign {code} @member`")

    @task.command(name="assign", description="Assign a task to a member (staff).")
    @commands.guild_only()
    @require_scope("tasks.assign")
    async def task_assign(self, ctx, task: str, member: discord.Member):
        task_doc = await self._find_task(task)
        if task_doc is None:
            await ctx.send(f"⚠️ No task matching **{task}**.")
            return
        if member == self.bot.user:
            await ctx.send("I do my own task tracking. 🤖")
            return
        task_doc["assignee"] = str(member.id)
        if task_doc.get("status") in (None, "", "open"):
            task_doc["status"] = "in_progress"
        task_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.save_task(task_doc)
        await ctx.send(f"👤 Assigned `{task_doc['task_id']}` "
                       f"**{task_doc.get('title')}** → {member.mention}")

    @task.command(name="complete", description="Mark your task as done.")
    @commands.guild_only()
    @require_scope("tasks.claim")
    async def task_complete(self, ctx, task: str):
        task_doc = await self._find_task(task)
        if task_doc is None:
            await ctx.send(f"⚠️ No task matching **{task}**.")
            return
        scopes = await scopes_for_author(ctx)
        if not ("tasks.assign" in scopes or
                str(task_doc.get("assignee")) == str(ctx.author.id)):
            await ctx.send("🔒 Only the assignee (or staff) can complete this task.")
            return
        task_doc["status"] = "done"
        task_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.save_task(task_doc)
        await ctx.send(f"✅ Completed **{task_doc.get('title')}** 🎉")

    @task.command(name="claim", description="Claim an unassigned task.")
    @commands.guild_only()
    @require_scope("tasks.claim")
    async def task_claim(self, ctx, task: str):
        task_doc = await self._find_task(task)
        if task_doc is None:
            await ctx.send(f"⚠️ No task matching **{task}**.")
            return
        if task_doc.get("assignee"):
            await ctx.send(f"🚩 `{task_doc['task_id']}` is already assigned "
                           f"to <@{task_doc['assignee']}>.")
            return
        task_doc["assignee"] = str(ctx.author.id)
        task_doc["status"] = "in_progress"
        task_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.save_task(task_doc)
        await ctx.send(f"🙋 Claimed **{task_doc.get('title')}** (`{task_doc['task_id']}`). "
                       "Good luck! 💪")

    @task.command(name="edit", description="Edit a task (staff).")
    @commands.guild_only()
    @require_scope("tasks.assign")
    async def task_edit(self, ctx, task: str, title: str = None,
                        priority: str = None, due: str = None,
                        status: str = None, cell: str = None):
        task_doc = await self._find_task(task)
        if task_doc is None:
            await ctx.send(f"⚠️ No task matching **{task}**.")
            return
        if title:
            task_doc["title"] = title.strip()
        if priority:
            task_doc["priority"] = _norm_priority(priority)
        if due:
            try:
                task_doc["due"] = parse_due(due)
            except ValueError:
                await ctx.send("⚠️ Bad due date — use YYYY-MM-DD, 'tomorrow' or 'in 3d'.")
                return
        if status:
            task_doc["status"] = _norm_status(status)
        if cell is not None:
            task_doc["cell"] = cell.strip()
        task_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.save_task(task_doc)
        await ctx.send(f"✏️ Updated `{task_doc['task_id']}` — **{task_doc.get('title')}**")

    @task.command(name="cancel", description="Cancel a task (staff).")
    @commands.guild_only()
    @require_scope("tasks.assign")
    async def task_cancel(self, ctx, task: str):
        task_doc = await self._find_task(task)
        if task_doc is None:
            await ctx.send(f"⚠️ No task matching **{task}**.")
            return
        task_doc["status"] = "cancelled"
        task_doc["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.save_task(task_doc)
        await ctx.send(f"🚫 Cancelled **{task_doc.get('title')}** (`{task_doc['task_id']}`).")


async def scopes_for_author_like(ctx_or_interaction):
    """Scope check usable from interactions too (task detail buttons)."""
    from cogs._scopes import scopes_for
    from cogs._scopes import is_bot_admin as _iba
    if _iba(getattr(ctx_or_interaction, "author", None) or
            getattr(ctx_or_interaction, "user", None)):
        return {"tasks.assign"}
    record = {}
    member = getattr(ctx_or_interaction, "author", None) or getattr(
        ctx_or_interaction, "user", None)
    try:
        record = (await store.get_member(member.id)) or {}
    except StoreError:
        pass
    return scopes_for(str(record.get("club_role") or "").lower(), is_member=member)


async def setup(bot):
    await bot.add_cog(Tasks(bot))