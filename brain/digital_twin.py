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
