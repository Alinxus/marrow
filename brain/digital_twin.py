"""Persistent desktop digital twin primitives."""

from __future__ import annotations

import time
from typing import Any

from storage import state_store


def init_twin() -> None:
    state_store.init_state_store()


def note_focus_change(app_name: str, window_title: str, url: str = "") -> dict[str, Any]:
    payload = state_store.load_twin()
    entities = payload.setdefault(
        "entities",
        {"apps": {}, "windows": {}, "tabs": {}, "files": {}, "people": {}, "tasks": {}},
    )
    ts = time.time()

    app_key = (app_name or "unknown").strip() or "unknown"
    window_key = (window_title or "untitled").strip() or "untitled"

    apps = entities.setdefault("apps", {})
    app_info = apps.setdefault(app_key, {"seen_count": 0, "last_seen": 0.0})
    app_info["seen_count"] += 1
    app_info["last_seen"] = ts

    windows = entities.setdefault("windows", {})
    win_info = windows.setdefault(
        window_key,
        {"app": app_key, "seen_count": 0, "last_seen": 0.0, "last_url": ""},
    )
    win_info["app"] = app_key
    win_info["seen_count"] += 1
    win_info["last_seen"] = ts
    if url:
        win_info["last_url"] = url
        tabs = entities.setdefault("tabs", {})
        tabs[url] = {
            "window": window_key,
            "app": app_key,
            "last_seen": ts,
        }

    event = {
        "ts": ts,
        "type": "focus_change",
        "app": app_key,
        "window_title": window_key,
        "url": url,
    }
    state_store.append_twin_event(event)
    state_store.save_twin(payload)
    return event


def add_task_signal(task_name: str, status: str, mission_id: str = "") -> dict[str, Any]:
    payload = state_store.load_twin()
    entities = payload.setdefault(
        "entities",
        {"apps": {}, "windows": {}, "tabs": {}, "files": {}, "people": {}, "tasks": {}},
    )
    ts = time.time()
    tasks = entities.setdefault("tasks", {})
    tasks[task_name] = {
        "status": status,
        "mission_id": mission_id,
        "last_seen": ts,
    }
    state_store.save_twin(payload)
    event = {"ts": ts, "type": "task_signal", "task": task_name, "status": status}
    state_store.append_twin_event(event)
    return event


def get_active_workspace_summary(limit: int = 6) -> str:
    payload = state_store.load_twin()
    entities = payload.get("entities", {}) or {}
    timeline = payload.get("timeline", []) or []
    lines: list[str] = ["[Desktop twin]"]

    recent_focus = [item for item in timeline[-12:] if item.get("type") == "focus_change"]
    if recent_focus:
        latest = recent_focus[-1]
        app = str(latest.get("app", "") or "").strip()
        title = str(latest.get("window_title", "") or "").strip()
        if app or title:
            lines.append(f"Current focus: {' | '.join(x for x in [app, title] if x)[:220]}")

    apps = entities.get("apps", {}) or {}
    if apps:
        ordered_apps = sorted(
            apps.items(),
            key=lambda kv: float((kv[1] or {}).get("last_seen", 0.0) or 0.0),
            reverse=True,
        )
        names = [str(name) for name, _ in ordered_apps[:limit] if str(name).strip()]
        if names:
            lines.append("Recent apps: " + ", ".join(names))

    tasks = entities.get("tasks", {}) or {}
    if tasks:
        ordered_tasks = sorted(
            tasks.items(),
            key=lambda kv: float((kv[1] or {}).get("last_seen", 0.0) or 0.0),
            reverse=True,
        )
        task_bits = []
        for name, meta in ordered_tasks[:limit]:
            status = str((meta or {}).get("status", "") or "").strip()
            task_bits.append(f"{name} ({status})" if status else str(name))
        if task_bits:
            lines.append("Task signals: " + "; ".join(task_bits))

    windows = entities.get("windows", {}) or {}
    if windows:
        ordered_windows = sorted(
            windows.items(),
            key=lambda kv: float((kv[1] or {}).get("last_seen", 0.0) or 0.0),
            reverse=True,
        )
        names = [str(name)[:120] for name, _ in ordered_windows[:3] if str(name).strip()]
        if names:
            lines.append("Recent windows: " + " | ".join(names))

    return "\n".join(lines[:5])
