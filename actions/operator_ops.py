"""Higher-level operator workflows for communications, docs, browser, projects, and verification."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable, Optional

from actions import browser, office, todo, web
from brain.digital_twin import get_active_workspace_summary
from storage import db


def _compact(text: str, limit: int = 220) -> str:
    return " ".join((text or "").split())[:limit]


def _recent_screen_summary() -> str:
    ctx = db.get_recent_context(15 * 60)
    shots = ctx.get("screenshots", [])
    if not shots:
        return "No recent screen context."
    latest = shots[0]
    parts = [
        str(latest.get("app_name", "") or "").strip(),
        str(latest.get("window_title", "") or "").strip(),
        _compact(str(latest.get("focused_context", "") or ""), 180),
        _compact(str(latest.get("ocr_text", "") or ""), 240),
    ]
    return " | ".join(part for part in parts if part)[:700]


async def communications_brief(get_emails_fn, get_calendar_fn) -> str:
    emails = get_emails_fn(24, 8, True)
    calendar = get_calendar_fn(1)
    reminders = await todo.reminder_list()
    tasks = await todo.todo_list("pending", 8)
    parts = [
        "## Communications Brief",
        emails[:1400],
        calendar[:1400],
        reminders[:800],
        tasks[:1000],
    ]
    return "\n\n".join(part for part in parts if part)


async def document_task(
    operation: str,
    path: str,
    *,
    content: str = "",
    sheet: str = "",
    page: int | None = None,
) -> str:
    op = (operation or "").strip().lower()
    suffix = Path(path).suffix.lower()

    if op in {"read", "summarize"}:
        if suffix in {".xlsx", ".xls"}:
            text = await office.excel_read(path, sheet or None)
        elif suffix == ".docx":
            text = await office.word_read(path)
        elif suffix == ".pdf":
            text = await office.pdf_read(path, page)
        else:
            p = Path(path).expanduser().resolve()
            if not p.exists():
                return f"[error] File not found: {path}"
            text = p.read_text(encoding="utf-8", errors="replace")
        if op == "summarize":
            return f"## Document Summary\nPath: {path}\n\n{text[:3000]}"
        return text[:6000]

    if op in {"write", "create"}:
        if suffix == ".docx":
            return await office.word_write(path, content)
        if suffix in {".xlsx", ".xls"}:
            return await office.excel_write(path, content, sheet or "Sheet1")
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"[file] Written to {p}"

    if op == "append":
        if suffix in {".xlsx", ".xls"}:
            return await office.excel_append(path, content, sheet or "Sheet1")
        p = Path(path).expanduser().resolve()
        previous = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        p.write_text(previous + content, encoding="utf-8")
        return f"[file] Appended to {p}"

    return f"[error] Unsupported document operation: {operation}"


async def browser_research(goal: str, query: str = "", url: str = "") -> str:
    parts = [f"## Browser Workflow\nGoal: {goal[:200]}"]
    if query:
        parts.append((await web.web_search(query, limit=5))[:1800])
    if url:
        parts.append((await web.web_extract(url, prompt=goal or "Extract what matters"))[:2200])
    if not query and not url:
        parts.append((await browser.browser_search(goal))[:1200])
    return "\n\n".join(part for part in parts if part)


async def computer_workflow(
    goal: str,
    *,
    command: str = "",
    app_name: str = "",
    run_command: Optional[Callable[[str, int], str]] = None,
) -> str:
    out = [f"## Computer Workflow\nGoal: {goal[:220]}"]
    if command and run_command:
        out.append(run_command(command, 45)[:2200])
    if app_name and run_command:
        out.append(run_command(f"Get-Process | Where-Object {{$_.ProcessName -like '*{app_name}*'}}", 15)[:1200])
    out.append(get_active_workspace_summary()[:1000])
    out.append(_recent_screen_summary())
    return "\n\n".join(part for part in out if part)


async def project_workflow(goal: str, repo_path: str = "", run_command: Optional[Callable[[str, int], str]] = None) -> str:
    repo = Path(repo_path).expanduser().resolve() if repo_path else None
    lines = [f"## Project Workflow\nGoal: {goal[:220]}"]
    if repo and repo.exists() and run_command:
        repo_str = str(repo).replace("'", "''")
        repo_cmd = (
            f"Set-Location '{repo_str}'; "
            "git status --short; "
            "git branch --show-current; "
            "Get-ChildItem -Force | Select-Object -First 12 Name,Length,LastWriteTime | Format-Table -AutoSize"
        )
        lines.append(run_command(repo_cmd, 30)[:2600])
    lines.append(get_active_workspace_summary()[:1000])
    return "\n\n".join(part for part in lines if part)


async def personal_workflow(kind: str, detail: str = "") -> str:
    kind_low = (kind or "").strip().lower()
    if kind_low in {"shopping", "errands", "travel", "household"}:
        await todo.todo_add(
            title=f"{kind_low.title()} admin",
            description=detail or f"Follow up on {kind_low}",
            priority=2,
        )
        await todo.reminder_add(f"Review {kind_low} admin", 2 * 3600)
        return f"[personal] Created a {kind_low} admin task and follow-up reminder."
    return f"[personal] Logged personal admin request: {detail[:180] or kind}"


async def verify_workspace_state(expectation: str = "") -> str:
    twin = get_active_workspace_summary()
    recent = _recent_screen_summary()
    status = "unknown"
    exp = " ".join((expectation or "").lower().split())
    hay = f"{twin}\n{recent}".lower()
    if exp:
        status = "matched" if exp and any(token in hay for token in exp.split()[:4]) else "not_matched"
    return "\n".join(
        [
            f"## Workspace Verification",
            f"Expectation: {expectation or 'none provided'}",
            f"Status: {status}",
            twin[:1200],
            recent[:1200],
        ]
    )
