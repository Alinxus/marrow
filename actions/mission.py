"""Mission mode: plan, execute, pause/resume, and rollback."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from storage import db

log = logging.getLogger(__name__)


def _safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _normalize_steps(raw_steps: Any) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    if not isinstance(raw_steps, list):
        return steps
    for i, s in enumerate(raw_steps, start=1):
        if isinstance(s, str):
            action = s.strip()
            if action:
                steps.append(
                    {
                        "title": f"Step {i}",
                        "action": action,
                        "rollback_action": "",
                    }
                )
            continue
        if isinstance(s, dict):
            action = str(s.get("action", "")).strip()
            if not action:
                continue
            steps.append(
                {
                    "title": str(s.get("title", f"Step {i}")).strip() or f"Step {i}",
                    "action": action,
                    "rollback_action": str(s.get("rollback_action", "")).strip(),
                }
            )
    return steps


async def _plan_steps(goal: str, context: str = "") -> list[dict[str, str]]:
    from brain.llm import get_client

    prompt = f"""Create a practical mission plan for this goal.

Goal: {goal}
Context: {context[:1000]}

Return ONLY a JSON array with 2-8 items.
Each item must be:
{{
  "title": "short step title",
  "action": "exact action request to execute",
  "rollback_action": "optional undo action, or empty string"
}}
"""
    try:
        llm = get_client()
        resp = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            model_type="scoring",
            max_tokens=900,
        )
        raw = (resp.text or "").strip()
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1]
        steps = _normalize_steps(_safe_json_loads(raw))
        if steps:
            return steps
    except Exception as e:
        log.warning(f"Mission planning fallback: {e}")

    return [
        {
            "title": "Execute goal",
            "action": goal,
            "rollback_action": "",
        }
    ]


async def mission_create(goal: str, plan_json: str = "", context: str = "") -> str:
    raw_steps = _safe_json_loads(plan_json) if plan_json.strip() else None
    steps = _normalize_steps(raw_steps)
    if not steps:
        steps = await _plan_steps(goal, context)

    mission_id = db.insert_mission(time.time(), goal)
    for idx, step in enumerate(steps, start=1):
        db.insert_mission_step(
            mission_id=mission_id,
            step_index=idx,
            title=step["title"],
            action=step["action"],
            rollback_action=step["rollback_action"],
        )
    db.set_mission_total_steps(mission_id, len(steps))
    return f"[mission] Created mission #{mission_id} with {len(steps)} steps"


def _format_mission_status(mission: dict, steps: list[dict]) -> str:
    done = sum(1 for s in steps if s.get("status") in ("completed", "rolled_back"))
    total = len(steps)
    lines = [
        f"Mission #{mission['id']} — {mission.get('status', 'unknown')}",
        f"Goal: {mission.get('goal', '')}",
        f"Progress: {done}/{total}",
    ]
    if mission.get("last_error"):
        lines.append(f"Last error: {mission['last_error'][:220]}")
    for s in steps:
        title = (s.get("title") or s.get("action") or "step")[:80]
        lines.append(f"- [{s.get('status', 'pending')}] {s['step_index']}. {title}")
    return "\n".join(lines)


async def mission_status(mission_id: int) -> str:
    mission = db.get_mission(mission_id)
    if not mission:
        return f"[error] Mission #{mission_id} not found"
    steps = db.get_mission_steps(mission_id)
    return _format_mission_status(mission, steps)


async def mission_list(limit: int = 10, status: str = "") -> str:
    rows = db.list_missions(limit=max(1, min(100, int(limit))), status=status)
    if not rows:
        return "No missions found."
    lines = ["## Missions"]
    for r in rows:
        lines.append(
            f"- #{r['id']} [{r.get('status', 'unknown')}] {r.get('goal', '')[:80]}"
        )
    return "\n".join(lines)


async def mission_pause(mission_id: int) -> str:
    mission = db.get_mission(mission_id)
    if not mission:
        return f"[error] Mission #{mission_id} not found"
    if mission.get("status") not in ("running", "planned"):
        return f"[mission] Mission #{mission_id} is {mission.get('status')}"
    db.update_mission_status(mission_id, "paused", mission.get("current_step", 0), "")
    return f"[mission] Paused mission #{mission_id}"


async def mission_start(mission_id: int, context: str = "") -> str:
    from actions.executor import execute_action

    mission = db.get_mission(mission_id)
    if not mission:
        return f"[error] Mission #{mission_id} not found"

    steps = db.get_mission_steps(mission_id)
    if not steps:
        return f"[error] Mission #{mission_id} has no steps"

    db.update_mission_status(mission_id, "running", mission.get("current_step", 0), "")

    for s in steps:
        latest = db.get_mission(mission_id) or {}
        if latest.get("status") == "paused":
            return f"[mission] Mission #{mission_id} paused at step {s['step_index']}"

        if s.get("status") == "completed":
            continue

        db.update_mission_step_status(s["id"], "running", started_ts=time.time())
        db.update_mission_status(mission_id, "running", s["step_index"], "")

        try:
            result = await execute_action(s["action"], context=context)
            result_text = (result or "").strip()
            if result_text.lower().startswith("[error]"):
                db.update_mission_step_status(
                    s["id"],
                    "failed",
                    result=result_text[:4000],
                    finished_ts=time.time(),
                )
                db.update_mission_status(
                    mission_id,
                    "failed",
                    s["step_index"],
                    result_text[:500],
                )
                return (
                    f"[mission] Failed at step {s['step_index']}: {result_text[:220]}"
                )

            db.update_mission_step_status(
                s["id"],
                "completed",
                result=result_text[:4000],
                finished_ts=time.time(),
            )
        except Exception as e:
            err = str(e)
            db.update_mission_step_status(
                s["id"],
                "failed",
                result=err[:4000],
                finished_ts=time.time(),
            )
            db.update_mission_status(mission_id, "failed", s["step_index"], err[:500])
            return f"[mission] Failed at step {s['step_index']}: {err[:220]}"

    db.update_mission_status(mission_id, "completed", len(steps), "")
    return f"[mission] Completed mission #{mission_id} ({len(steps)} steps)"


async def mission_resume(mission_id: int, context: str = "") -> str:
    mission = db.get_mission(mission_id)
    if not mission:
        return f"[error] Mission #{mission_id} not found"
    if mission.get("status") != "paused":
        return f"[mission] Mission #{mission_id} is not paused (status={mission.get('status')})"
    return await mission_start(mission_id, context=context)


async def mission_rollback(mission_id: int, steps: int = 1, context: str = "") -> str:
    from actions.executor import execute_action

    mission = db.get_mission(mission_id)
    if not mission:
        return f"[error] Mission #{mission_id} not found"

    all_steps = db.get_mission_steps(mission_id)
    completed = [s for s in all_steps if s.get("status") == "completed"]
    if not completed:
        return f"[mission] No completed steps to roll back for mission #{mission_id}"

    target = list(reversed(completed[-max(1, int(steps)) :]))
    done = 0
    skipped = 0
    failed = 0

    for s in target:
        rollback_action = (s.get("rollback_action") or "").strip()
        if not rollback_action:
            skipped += 1
            continue
        try:
            res = await execute_action(rollback_action, context=context)
            txt = (res or "").strip()
            if txt.lower().startswith("[error]"):
                failed += 1
                continue
            db.update_mission_step_status(
                s["id"],
                "rolled_back",
                result=f"[rollback] {txt[:3600]}",
                finished_ts=time.time(),
            )
            done += 1
        except Exception:
            failed += 1

    if failed:
        db.update_mission_status(
            mission_id,
            "rollback_failed",
            mission.get("current_step", 0),
            "rollback error",
        )
    else:
        db.update_mission_status(
            mission_id, "rolled_back", mission.get("current_step", 0), ""
        )
    return f"[mission] Rollback for #{mission_id}: done={done}, skipped={skipped}, failed={failed}"
