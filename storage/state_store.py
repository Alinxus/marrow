"""Versioned JSON state store for persistent Marrow runtime data."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".marrow"
STATE_VERSION = 1

MISSION_FILE = STATE_DIR / "missions.json"
TWIN_FILE = STATE_DIR / "twin.json"
GRAPH_FILE = STATE_DIR / "graph.json"
SKILLS_FILE = STATE_DIR / "skills.json"

_LOCK = threading.RLock()


def _default_payload(kind: str) -> dict[str, Any]:
    key = "items" if kind in {"skills", "graph"} else kind
    if kind == "missions":
        key = "missions"
    elif kind == "twin":
        key = "timeline"
    return {
        "schema_version": STATE_VERSION,
        "kind": kind,
        "updated_at": time.time(),
        key: [],
    }


def _read_json(path: Path, kind: str) -> dict[str, Any]:
    with _LOCK:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            payload = _default_payload(kind)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            return payload
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = _default_payload(kind)
        if not isinstance(payload, dict):
            payload = _default_payload(kind)
        payload.setdefault("schema_version", STATE_VERSION)
        payload.setdefault("kind", kind)
        payload.setdefault("updated_at", time.time())
        return payload


def _write_json(path: Path, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload["schema_version"] = STATE_VERSION
        payload["kind"] = kind
        payload["updated_at"] = time.time()
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload


def init_state_store() -> None:
    """Ensure all persistent state files exist."""
    _read_json(MISSION_FILE, "missions")
    twin = _read_json(TWIN_FILE, "twin")
    twin.setdefault("timeline", [])
    twin.setdefault("entities", {"apps": {}, "windows": {}, "tabs": {}, "files": {}, "people": {}, "tasks": {}})
    _write_json(TWIN_FILE, "twin", twin)
    graph = _read_json(GRAPH_FILE, "graph")
    graph.setdefault("items", [])
    graph.setdefault("edges", [])
    _write_json(GRAPH_FILE, "graph", graph)
    skills = _read_json(SKILLS_FILE, "skills")
    skills.setdefault("items", [])
    _write_json(SKILLS_FILE, "skills", skills)


def load_missions() -> dict[str, Any]:
    payload = _read_json(MISSION_FILE, "missions")
    payload.setdefault("missions", [])
    return payload


def save_missions(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("missions", [])
    return _write_json(MISSION_FILE, "missions", payload)


def upsert_mission(mission: dict[str, Any]) -> dict[str, Any]:
    payload = load_missions()
    missions = payload.setdefault("missions", [])
    mission_id = mission.get("mission_id")
    if mission_id:
        for index, existing in enumerate(missions):
            if existing.get("mission_id") == mission_id:
                missions[index] = mission
                return save_missions(payload)
    missions.append(mission)
    return save_missions(payload)


def get_mission(mission_id: str) -> dict[str, Any] | None:
    for mission in load_missions().get("missions", []):
        if mission.get("mission_id") == mission_id:
            return mission
    return None


def load_twin() -> dict[str, Any]:
    payload = _read_json(TWIN_FILE, "twin")
    payload.setdefault("timeline", [])
    payload.setdefault(
        "entities",
        {"apps": {}, "windows": {}, "tabs": {}, "files": {}, "people": {}, "tasks": {}},
    )
    return payload


def save_twin(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("timeline", [])
    payload.setdefault(
        "entities",
        {"apps": {}, "windows": {}, "tabs": {}, "files": {}, "people": {}, "tasks": {}},
    )
    return _write_json(TWIN_FILE, "twin", payload)


def append_twin_event(event: dict[str, Any], max_events: int = 500) -> dict[str, Any]:
    payload = load_twin()
    timeline = payload.setdefault("timeline", [])
    timeline.append(event)
    if len(timeline) > max_events:
        del timeline[: len(timeline) - max_events]
    return save_twin(payload)


def load_graph() -> dict[str, Any]:
    payload = _read_json(GRAPH_FILE, "graph")
    payload.setdefault("items", [])
    payload.setdefault("edges", [])
    return payload


def save_graph(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("items", [])
    payload.setdefault("edges", [])
    return _write_json(GRAPH_FILE, "graph", payload)


def load_skills() -> dict[str, Any]:
    payload = _read_json(SKILLS_FILE, "skills")
    payload.setdefault("items", [])
    return payload


def save_skills(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("items", [])
    return _write_json(SKILLS_FILE, "skills", payload)
