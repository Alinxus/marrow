"""Reasoning-aware execution planning and status tracking."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from brain.digital_twin import add_task_signal
from brain.context_engine import build_reasoning_context
from brain.llm import get_client
from storage import state_store

log = logging.getLogger(__name__)


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return {}


def _normalize_list(items: Any, limit: int = 6, item_limit: int = 160) -> list[str]:
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text[:item_limit])
        if len(out) >= limit:
            break
    return out


def _merge_unique(base: list[str], incoming: list[str], limit: int = 8) -> list[str]:
    out: list[str] = []
    for item in list(base or []) + list(incoming or []):
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _fallback_plan(user_text: str) -> dict[str, Any]:
    low = " ".join((user_text or "").lower().split())
    complex_markers = (
        " and ",
        " then ",
        "compare",
        "research",
        "plan",
        "figure out",
        "build",
        "debug",
        "investigate",
        "set up",
        "walk through",
    )
    mode = "complex" if len(low.split()) >= 12 or any(x in low for x in complex_markers) else "direct"
    return {
        "mode": mode,
        "action_request": user_text.strip(),
        "goal": user_text.strip(),
        "success_criteria": [],
        "risks": [],
        "verification_checks": [],
        "next_steps": [],
        "recommended_tools": [],
        "action_strategy": "",
        "clarifying_question": "",
    }


def _persist_execution_plan(
    session_id: str,
    plan: dict[str, Any],
    *,
    stage: str,
    status: str,
) -> dict[str, Any]:
    session = state_store.get_scratchpad_session(session_id)
    mode = str(plan.get("mode", "") or "").strip().lower()
    goal = str(plan.get("goal", "") or "").strip()
    strategy = str(plan.get("action_strategy", "") or "").strip()
    success = _normalize_list(plan.get("success_criteria", []), limit=6)
    risks = _normalize_list(plan.get("risks", []), limit=4)
    checks = _normalize_list(plan.get("verification_checks", []), limit=5)
    next_steps = _normalize_list(plan.get("next_steps", []), limit=6)
    tools = _normalize_list(plan.get("recommended_tools", []), limit=5)

    session["active_mode"] = "execute"
    if goal:
        session["problem_summary"] = goal[:800]
        session["goals"] = _merge_unique(_normalize_list(session.get("goals", []), limit=8), [goal], limit=8)
    if strategy:
        session["action_strategy"] = strategy[:220]
    if success:
        session["success_criteria"] = success
    if next_steps:
        session["next_steps"] = _merge_unique(_normalize_list(session.get("next_steps", []), limit=8), next_steps, limit=8)
    if tools:
        session["recommended_tools"] = _merge_unique(_normalize_list(session.get("recommended_tools", []), limit=8), tools, limit=8)
    session["execution_status"] = {
        "status": status,
        "mode": mode,
        "goal": goal[:220],
        "checks": checks,
        "risks": risks,
        "summary": strategy[:220],
        "updated_at": time.time(),
    }
    state_store.upsert_scratchpad_session(session_id, session)
    try:
        from brain.deep_reasoning import publish_reasoning_workbench

        publish_reasoning_workbench(
            session,
            stage=stage,
            status=status,
            extra={"execution_plan": plan},
        )
    except Exception as exc:
        log.debug(f"Execution workbench publish skipped: {exc}")
    return session


async def prepare_reasoned_action(
    user_text: str,
    context_hint: str = "",
    session_id: str = "default",
) -> dict[str, Any]:
    """Turn an action request into a more deliberate execution brief."""
    selected = await build_reasoning_context(
        user_text,
        context_hint=context_hint,
        session_id=session_id,
    )
    assembled_context = selected.get("assembled_context", "") or context_hint
    llm = get_client()
    if llm.provider == "none":
        plan = _fallback_plan(user_text)
        _persist_execution_plan(session_id, plan, stage="execution_plan", status="ready")
        return plan

    prompt = f"""Prepare an execution brief for this user request.

Return strict JSON only:
{{
  "mode": "direct" | "complex" | "clarify",
  "action_request": "",
  "goal": "",
  "success_criteria": [],
  "risks": [],
  "verification_checks": [],
  "next_steps": [],
  "recommended_tools": [],
  "action_strategy": "",
  "clarifying_question": ""
}}

Rules:
- Use "direct" when one action or one tool chain is probably enough.
- Use "complex" when the request likely needs planning, several steps, research, verification, or recovery.
- Use "clarify" only if a missing detail blocks useful execution.
- Rewrite action_request as a concise executable instruction.
- success_criteria should say what done looks like.
- action_strategy should be a short implementation approach, not a generic summary.

User request:
{user_text}

Available context:
{assembled_context[:4500]}
"""
    try:
        resp = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            model_type="reasoning",
            max_tokens=420,
        )
        plan = _fallback_plan(user_text) | _parse_json_object(resp.text)
    except Exception as exc:
        log.debug(f"Reasoned action planning fallback: {exc}")
        plan = _fallback_plan(user_text)

    plan["mode"] = str(plan.get("mode", "direct") or "direct").strip().lower()
    if plan["mode"] not in {"direct", "complex", "clarify"}:
        plan["mode"] = "direct"
    plan["action_request"] = str(plan.get("action_request", "") or user_text).strip()[:600]
    plan["goal"] = str(plan.get("goal", "") or user_text).strip()[:600]
    plan["action_strategy"] = str(plan.get("action_strategy", "") or "").strip()[:220]
    plan["clarifying_question"] = str(plan.get("clarifying_question", "") or "").strip()[:220]
    plan["success_criteria"] = _normalize_list(plan.get("success_criteria", []), limit=6)
    plan["risks"] = _normalize_list(plan.get("risks", []), limit=4)
    plan["verification_checks"] = _normalize_list(plan.get("verification_checks", []), limit=5)
    plan["next_steps"] = _normalize_list(plan.get("next_steps", []), limit=6)
    plan["recommended_tools"] = _normalize_list(plan.get("recommended_tools", []), limit=5)

    status = "awaiting_input" if plan["mode"] == "clarify" else "ready"
    add_task_signal(plan["goal"][:140], status)
    _persist_execution_plan(session_id, plan, stage="execution_plan", status=status)
    return plan


async def finalize_reasoned_action(
    plan: dict[str, Any],
    result_text: str,
    session_id: str = "default",
) -> None:
    """Record execution outcome and verification hints in the scratchpad."""
    session = state_store.get_scratchpad_session(session_id)
    low = (result_text or "").lower()
    failed = any(marker in low for marker in ("[error", "failed", "exception", "could not", "unable to"))
    checks = _normalize_list(plan.get("verification_checks", []), limit=5)
    risks = _normalize_list(plan.get("risks", []), limit=4)
    session["active_mode"] = "execute"
    session["execution_status"] = {
        "status": "needs_review" if failed else "completed",
        "mode": str(plan.get("mode", "") or "").strip().lower(),
        "goal": str(plan.get("goal", "") or "")[:220],
        "checks": checks,
        "risks": risks,
        "summary": str(result_text or "")[:240],
        "updated_at": time.time(),
    }
    session["verification_status"] = {
        "status": "review_needed" if failed else "pending_checks",
        "issues": risks if failed else [],
        "checks": checks,
    }
    if checks:
        session["next_steps"] = _merge_unique(
            _normalize_list(session.get("next_steps", []), limit=8),
            checks,
            limit=8,
        )
    add_task_signal(str(plan.get("goal", "") or "")[:140], session["execution_status"]["status"])
    state_store.upsert_scratchpad_session(session_id, session)
    try:
        from brain.deep_reasoning import publish_reasoning_workbench

        publish_reasoning_workbench(
            session,
            stage="execution_result",
            status="needs_review" if failed else "complete",
            extra={"execution_result": (result_text or "")[:800]},
        )
    except Exception as exc:
        log.debug(f"Execution result publish skipped: {exc}")
