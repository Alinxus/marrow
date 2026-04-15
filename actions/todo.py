"""
Todo and task tracking system.

Manages tasks, reminders, and follow-ups.
Uses SQLite for local persistence (no external service needed).
"""

import asyncio
import json
import logging
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import config
from storage import db

log = logging.getLogger(__name__)


async def todo_add(
    title: str,
    description: str = "",
    due: Optional[str] = None,
    priority: int = 3,
    tags: list = None,
) -> str:
    """Add a new task."""
    ts = datetime.now().timestamp()

    # Store in DB
    db.insert_todo(
        ts=ts,
        title=title,
        description=description,
        due_ts=datetime.fromisoformat(due).timestamp() if due else None,
        priority=priority,
        tags=json.dumps(tags or []),
        status="pending",
    )

    log.info(f"Todo added: {title}")
    return f"[todo] Added: {title}"


async def todo_list(status: str = "pending", limit: int = 20) -> str:
    """List tasks."""
    todos = db.get_todos(status=status, limit=limit)

    if not todos:
        return f"No {status} tasks."

    output = [f"## {status.title()} Tasks\n"]
    for t in todos:
        due_str = ""
        if t.get("due_ts"):
            due = datetime.fromtimestamp(t["due_ts"])
            due_str = f" (due: {due.strftime('%Y-%m-%d %H:%M')})"

        priority_str = ["🔴", "🟠", "🟡", "✅"][t.get("priority", 3)]
        output.append(f"- {priority_str} {t['title']}{due_str}")
        if t.get("description"):
            output.append(f"  {t['description'][:100]}")

    return "\n".join(output)


async def todo_complete(todo_id: int) -> str:
    """Mark task as complete."""
    db.update_todo_status(todo_id, "completed")
    return f"[todo] Completed task {todo_id}"


async def todo_delete(todo_id: int) -> str:
    """Delete a task."""
    db.delete_todo(todo_id)
    return f"[todo] Deleted task {todo_id}"


async def todo_search(query: str) -> str:
    """Search tasks."""
    todos = db.search_todos(query)

    if not todos:
        return f"No tasks matching: {query}"

    output = [f"## Tasks matching '{query}'\n"]
    for t in todos:
        output.append(f"- {t['title']} [{t['status']}]")

    return "\n".join(output)


async def reminder_add(
    message: str,
    trigger_after: int = 60,  # seconds
    action: Optional[str] = None,
) -> str:
    """Schedule a reminder."""
    ts = datetime.now().timestamp()
    trigger_ts = ts + trigger_after

    db.insert_reminder(
        ts=ts,
        trigger_ts=trigger_ts,
        message=message,
        action=action,
        status="pending",
    )

    return f"[reminder] Scheduled: {message[:50]}... in {trigger_after}s"


async def reminder_list() -> str:
    """List pending reminders."""
    reminders = db.get_pending_reminders()

    if not reminders:
        return "No pending reminders."

    output = ["## Pending Reminders\n"]
    for r in reminders:
        trigger = datetime.fromtimestamp(r["trigger_ts"])
        output.append(f"- {r['message']} (at {trigger.strftime('%H:%M:%S')})")

    return "\n".join(output)


async def reminder_cancel(reminder_id: int) -> str:
    """Cancel a reminder."""
    db.update_reminder_status(reminder_id, "cancelled")
    return f"[reminder] Cancelled {reminder_id}"


def check_reminders() -> list:
    """Check for due reminders (called by main loop)."""
    now = datetime.now().timestamp()
    reminders = db.get_due_reminders(now)

    due = []
    for r in reminders:
        db.update_reminder_status(r["id"], "triggered")
        due.append(
            {
                "message": r["message"],
                "action": r.get("action"),
            }
        )

    return due
