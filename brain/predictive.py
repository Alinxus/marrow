"""Predictive pre-actions built from the desktop twin and mission state."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

import config
from storage import state_store

log = logging.getLogger(__name__)

_last_suggestion_at = 0.0
_last_signature = ""


def _emit_suggestion(title: str, body: str, actions: list[str], urgency: int = 3) -> None:
    payload = {
        "kind": "suggestion",
        "title": title,
        "body": body,
        "actions": actions,
        "urgency": urgency,
        "ts": time.time(),
    }
    try:
        from ui.bridge import get_bridge

        bridge = get_bridge()
        bridge.overlay_update.emit(json.dumps(payload))
        bridge.toast_requested.emit(title, body, urgency)
    except Exception as exc:
        log.debug(f"Predictive emit skipped: {exc}")


def _latest_paused_mission() -> dict | None:
    missions = state_store.load_missions().get("missions", [])
    for mission in reversed(missions):
        if mission.get("state") == "paused":
            return mission
    return None


def _active_app_signal() -> str:
    twin = state_store.load_twin()
    timeline = twin.get("timeline", [])
    if not timeline:
        return ""
    latest = timeline[-1]
    if latest.get("type") == "focus_change":
        return str(latest.get("app", ""))
    return ""


def _build_suggestion() -> tuple[str, str, list[str], int] | None:
    now = datetime.now()
    paused = _latest_paused_mission()
    active_app = _active_app_signal().lower()

    if paused:
        return (
            "Paused Mission Ready",
            f"Resume: {paused.get('goal', '')[:120]}",
            ["/mission resume", "/mission status"],
            3,
        )

    if 8 <= now.hour <= 11 and active_app in {"code", "cursor", "terminal", "powershell"}:
        return (
            "Coding Session Detected",
            "Marrow can prep your next move: resume the last mission, run a swarm pass, or summarize recent context.",
            ["/mission status", "/swarm status"],
            3,
        )

    twin = state_store.load_twin()
    timeline = twin.get("timeline", [])
    if len(timeline) >= 4:
        recent = timeline[-4:]
        apps = [item.get("app", "") for item in recent if item.get("type") == "focus_change"]
        if len(apps) >= 3 and len(set(apps[-3:])) == 1 and apps[-1]:
            return (
                "Deep Focus Detected",
                f"You've been anchored in {apps[-1]} for a while. Marrow can stay quiet or prep a verification/summarization pass.",
                ["/mission status"],
                2,
            )
    return None


async def predictive_loop() -> None:
    global _last_suggestion_at, _last_signature

    if not config.PREDICTIVE_ENABLED:
        return

    await asyncio.sleep(20)
    while True:
        try:
            suggestion = _build_suggestion()
            if suggestion:
                title, body, actions, urgency = suggestion
                signature = f"{title}|{body}"
                if signature != _last_signature or (time.time() - _last_suggestion_at) > 900:
                    _emit_suggestion(title, body, actions, urgency)
                    _last_signature = signature
                    _last_suggestion_at = time.time()
        except Exception as exc:
            log.debug(f"Predictive tick error: {exc}")
        await asyncio.sleep(max(30, config.PREDICTIVE_INTERVAL_SECONDS))
