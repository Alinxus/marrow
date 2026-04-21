"""General-purpose deep reasoning orchestration for hard, long-horizon problems."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from actions.code_exec import code_run, eval_expression
from actions.memory import memory_get_context
from actions.web import web_search
from brain.llm import get_client
from storage import db, state_store
from ui.bridge import get_bridge

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


def _normalize_list(items: Any, limit: int = 8, item_limit: int = 220) -> list[str]:
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


def _merge_unique(base: list[str], incoming: list[str], limit: int = 10) -> list[str]:
    out: list[str] = []
    for item in list(base or []) + list(incoming or []):
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _scratchpad_text(session: dict[str, Any]) -> str:
    lines = [
        f"Title: {session.get('problem_title', '')}",
        f"Summary: {session.get('problem_summary', '')}",
        f"Project: {session.get('project_brief', '')}",
        f"Domain: {session.get('domain', 'general')}",
        f"Task type: {session.get('task_type', 'analyze')}",
    ]
    for key in (
        "goals",
        "learning_goals",
        "constraints",
        "assumptions",
        "unknowns",
        "concepts",
        "attempted_approaches",
        "dead_ends",
        "decisions",
        "design_decisions",
        "experiments",
        "blockers",
        "teaching_notes",
        "recommended_tools",
        "open_questions",
        "next_steps",
    ):
        values = _normalize_list(session.get(key, []), limit=8)
        if values:
            lines.append(f"{key.replace('_', ' ').title()}:")
            lines.extend(f"- {v}" for v in values)
    return "\n".join(lines)


def _emit_workbench(
    session: dict[str, Any],
    stage: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> None:
    payload = {
        "stage": stage,
        "status": status,
        "problem_title": session.get("problem_title", ""),
        "problem_summary": session.get("problem_summary", ""),
        "project_brief": session.get("project_brief", ""),
        "domain": session.get("domain", "general"),
        "task_type": session.get("task_type", "analyze"),
        "goals": _normalize_list(session.get("goals", []), limit=4),
        "learning_goals": _normalize_list(session.get("learning_goals", []), limit=4),
        "assumptions": _normalize_list(session.get("assumptions", []), limit=4),
        "unknowns": _normalize_list(session.get("unknowns", []), limit=4),
        "blockers": _normalize_list(session.get("blockers", []), limit=4),
        "concepts": _normalize_list(session.get("concepts", []), limit=4),
        "teaching_notes": _normalize_list(session.get("teaching_notes", []), limit=4),
        "design_decisions": _normalize_list(session.get("design_decisions", []), limit=4),
        "experiments": _normalize_list(session.get("experiments", []), limit=4),
        "recommended_tools": _normalize_list(session.get("recommended_tools", []), limit=4),
        "open_questions": _normalize_list(session.get("open_questions", []), limit=4),
        "next_steps": _normalize_list(session.get("next_steps", []), limit=4),
        "verification_status": session.get("verification_status", {}),
    }
    if extra:
        payload.update(extra)
    try:
        get_bridge().deep_reasoning_update.emit(json.dumps(payload))
    except Exception:
        pass


def _recent_history_text(session: dict[str, Any], limit: int = 8) -> str:
    items = session.get("history", [])
    if not isinstance(items, list):
        return ""
    lines = []
    for row in items[-limit:]:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role", "user")).strip() or "user"
        content = str(row.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content[:260]}")
    return "\n".join(lines)


def _domain_scaffold(domain: str, task_type: str) -> str:
    domain = (domain or "general").lower()
    task_type = (task_type or "analyze").lower()

    common = (
        "Work like a high-agency collaborator. Track assumptions, uncertainty, tradeoffs, "
        "and verification explicitly. Prefer concrete intermediate structure over vague advice."
    )
    scaffolds = {
        "software": (
            "For software problems: identify symptoms, likely causes, constraints, candidate fixes, "
            "testing strategy, edge cases, and regression risks."
        ),
        "engineering": (
            "For engineering problems: identify requirements, governing constraints, failure modes, "
            "approximations, design tradeoffs, and validation steps."
        ),
        "math": (
            "For math problems: define givens, unknowns, equations, method choice, derivation path, "
            "and a final check of algebra and units where relevant."
        ),
        "physics": (
            "For physics problems: identify known quantities, unknowns, governing principles, simplifying "
            "assumptions, unit consistency, limiting cases, and whether the answer is physically plausible."
        ),
        "research": (
            "For research problems: separate claims from evidence, note uncertainty, compare sources, and "
            "state what remains unknown."
        ),
        "strategy": (
            "For planning or strategy: define objective, options, constraints, risks, dependencies, and "
            "recommended next moves."
        ),
        "writing": (
            "For writing or communication: identify audience, goal, tone, required facts, and strongest structure."
        ),
        "general": (
            "For general problems: define the real question, constraints, candidate approaches, and concrete next steps."
        ),
    }
    task_specific = {
        "debug": "Focus on root cause, observables, instrumentation, and fastest falsifiable checks.",
        "design": "Focus on architecture, interfaces, constraints, tradeoffs, and failure modes.",
        "derive": "Focus on rigorous stepwise derivation and explicit assumptions.",
        "decide": "Compare options, make a recommendation, and justify it under uncertainty.",
        "plan": "Break the work into phases, dependencies, verification, and next actions.",
        "explain": "Optimize for clear teaching, intuition, and minimal ambiguity.",
        "analyze": "Build a model of the situation and test it against the evidence.",
    }
    return " ".join(
        [
            common,
            scaffolds.get(domain, scaffolds["general"]),
            task_specific.get(task_type, task_specific["analyze"]),
        ]
    )


def _heuristic_mode(user_text: str) -> dict[str, Any]:
    low = " ".join((user_text or "").lower().split())
    if not low:
        return {
            "mode": "quick",
            "confidence": 1.0,
            "domain": "general",
            "task_type": "analyze",
            "needs_clarification": False,
            "clarifying_question": "",
            "reason": "empty",
        }
    deep_markers = (
        "why does",
        "how does",
        "design",
        "architecture",
        "tradeoff",
        "trade-off",
        "debug",
        "root cause",
        "derive",
        "equation",
        "prove",
        "optimize",
        "system",
        "strategy",
        "plan",
        "physics",
        "math",
        "engineering",
        "model",
        "simulate",
        "uncertainty",
        "constraint",
        "failure mode",
        "what should we do",
        "what would you do",
    )
    domain = "general"
    if any(x in low for x in ("code", "bug", "api", "function", "class", "repo", "python", "typescript")):
        domain = "software"
    elif any(x in low for x in ("force", "energy", "velocity", "acceleration", "mass", "momentum", "circuit", "voltage")):
        domain = "physics"
    elif any(x in low for x in ("integral", "derivative", "matrix", "equation", "theorem", "proof", "probability")):
        domain = "math"
    elif any(x in low for x in ("design", "tolerance", "load", "stress", "thermal", "mechanical", "electrical", "control system")):
        domain = "engineering"
    elif any(x in low for x in ("market", "strategy", "roadmap", "decision", "option")):
        domain = "strategy"

    task_type = "analyze"
    if any(x in low for x in ("debug", "broken", "error", "failing", "root cause")):
        task_type = "debug"
    elif any(x in low for x in ("design", "architect", "structure")):
        task_type = "design"
    elif any(x in low for x in ("derive", "prove", "show that")):
        task_type = "derive"
    elif any(x in low for x in ("decide", "choose", "pick", "which should")):
        task_type = "decide"
    elif any(x in low for x in ("plan", "roadmap", "steps")):
        task_type = "plan"
    elif any(x in low for x in ("explain", "teach me", "walk me through")):
        task_type = "explain"

    is_deep = len(low.split()) >= 14 or any(marker in low for marker in deep_markers)
    return {
        "mode": "deep" if is_deep else "quick",
        "confidence": 0.7 if is_deep else 0.55,
        "domain": domain,
        "task_type": task_type,
        "needs_clarification": False,
        "clarifying_question": "",
        "reason": "heuristic",
    }


async def decide_reasoning_mode(
    user_text: str,
    context_hint: str = "",
    session_id: str = "default",
) -> dict[str, Any]:
    heuristic = _heuristic_mode(user_text)
    try:
        llm = get_client()
        if llm.provider == "none":
            return heuristic
        scratch = state_store.get_scratchpad_session(session_id)
        prompt = f"""Classify whether this user turn needs deep reasoning or a quick response.

Return strict JSON only:
{{
  "mode": "quick" | "deep",
  "confidence": 0.0,
  "domain": "general" | "software" | "engineering" | "math" | "physics" | "research" | "strategy" | "writing",
  "task_type": "analyze" | "debug" | "design" | "derive" | "decide" | "plan" | "explain",
  "needs_clarification": true | false,
  "clarifying_question": "<short question or empty>",
  "reason": "<short>"
}}

Use "deep" when the turn benefits from multi-step reasoning, maintaining assumptions, structured analysis, or long-horizon collaboration.
Use "quick" for normal chat, short status questions, or lightweight answers.
Only set needs_clarification when missing information blocks useful progress.

Current scratchpad:
{_scratchpad_text(scratch)[:1600]}

Recent turn history:
{_recent_history_text(scratch)}

Current context:
{(context_hint or "None")[:1200]}

User turn:
{user_text}
"""
        resp = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            model_type="scoring",
            max_tokens=220,
        )
        data = _parse_json_object(resp.text)
        if str(data.get("mode", "")).strip().lower() not in {"quick", "deep"}:
            return heuristic
        out = heuristic | data
        out["mode"] = str(out.get("mode", heuristic["mode"])).strip().lower()
        out["domain"] = str(out.get("domain", heuristic["domain"])).strip().lower() or heuristic["domain"]
        out["task_type"] = str(out.get("task_type", heuristic["task_type"])).strip().lower() or heuristic["task_type"]
        out["needs_clarification"] = bool(out.get("needs_clarification", False))
        out["clarifying_question"] = str(out.get("clarifying_question", "") or "").strip()
        out["reason"] = str(out.get("reason", "") or "").strip()
        try:
            out["confidence"] = float(out.get("confidence", heuristic["confidence"]) or 0.0)
        except Exception:
            out["confidence"] = heuristic["confidence"]
        return out
    except Exception as exc:
        log.debug(f"Deep reasoning classifier fallback: {exc}")
        return heuristic


async def _frame_problem(
    user_text: str,
    context_hint: str,
    session: dict[str, Any],
    mode: dict[str, Any],
) -> dict[str, Any]:
    llm = get_client()
    if llm.provider == "none":
        return {
            "problem_title": user_text[:80],
            "problem_summary": user_text[:220],
            "project_brief": "",
            "domain": mode.get("domain", "general"),
            "task_type": mode.get("task_type", "analyze"),
            "goals": [user_text[:220]],
            "learning_goals": [],
            "constraints": [],
            "assumptions": [],
            "unknowns": [],
            "concepts": [],
            "open_questions": [],
            "should_reset_session": False,
            "needs_clarification": mode.get("needs_clarification", False),
            "clarifying_question": mode.get("clarifying_question", ""),
        }

    prompt = f"""Frame the user's problem for a persistent reasoning scratchpad.

Return strict JSON only:
{{
  "problem_title": "",
  "problem_summary": "",
  "project_brief": "",
  "domain": "{mode.get('domain', 'general')}",
  "task_type": "{mode.get('task_type', 'analyze')}",
  "goals": [],
  "learning_goals": [],
  "constraints": [],
  "assumptions": [],
  "unknowns": [],
  "concepts": [],
  "open_questions": [],
  "should_reset_session": true | false,
  "needs_clarification": true | false,
  "clarifying_question": ""
}}

Set should_reset_session=true only if this is clearly a different problem than the current scratchpad.
Keep each list concise and useful.

Current scratchpad:
{_scratchpad_text(session)[:1800]}

Recent history:
{_recent_history_text(session)}

Context:
{(context_hint or "None")[:1500]}

User turn:
{user_text}
"""
    resp = await llm.create(
        messages=[{"role": "user", "content": prompt}],
        model_type="reasoning",
        max_tokens=360,
    )
    return _parse_json_object(resp.text)


async def _gather_supporting_material(
    user_text: str,
    context_hint: str,
    session: dict[str, Any],
    mode: dict[str, Any],
) -> str:
    llm = get_client()
    if llm.provider == "none":
        return ""
    prompt = f"""Decide what supporting evidence or computation would help answer this problem.

Return strict JSON only:
{{
  "memory_query": "",
  "web_query": "",
  "calculation_expression": "",
  "python_snippet": "",
  "reason": ""
}}

Rules:
- Use memory_query for retrieving relevant prior local context or retained facts.
- Use web_query only if external information is likely needed.
- Use calculation_expression only for a simple arithmetic/algebraic expression.
- Use python_snippet only if a short, side-effect-free calculation, simulation, or transformation would help.
- Leave fields empty if not needed.
- Never request shell commands, file edits, or actions here.

Scratchpad:
{_scratchpad_text(session)[:1400]}

Context:
{(context_hint or "None")[:1000]}

User turn:
{user_text}
"""
    resp = await llm.create(
        messages=[{"role": "user", "content": prompt}],
        model_type="scoring",
        max_tokens=220,
    )
    request = _parse_json_object(resp.text)
    chunks: list[str] = []

    memory_query = str(request.get("memory_query", "") or "").strip()
    if memory_query:
        try:
            local_hits = db.search_all(memory_query, limit=5)
            mem_ctx = await memory_get_context(memory_query, session_id=session.get("session_id", "default"))
            chunks.append(f"[Memory query] {memory_query}")
            if local_hits:
                chunks.append(f"[Local memory]\n{json.dumps(local_hits, ensure_ascii=False)[:1800]}")
            if mem_ctx:
                chunks.append(f"[Retained context]\n{mem_ctx[:1800]}")
        except Exception as exc:
            log.debug(f"Deep reasoning memory gather skipped: {exc}")

    web_query = str(request.get("web_query", "") or "").strip()
    if web_query:
        try:
            web_results = await web_search(web_query, limit=5)
            chunks.append(f"[Web query] {web_query}\n{web_results[:2200]}")
        except Exception as exc:
            log.debug(f"Deep reasoning web gather skipped: {exc}")

    expr = str(request.get("calculation_expression", "") or "").strip()
    if expr:
        try:
            calc = await eval_expression(expr)
            chunks.append(f"[Calculation] {expr} = {calc[:500]}")
        except Exception as exc:
            log.debug(f"Deep reasoning calc skipped: {exc}")

    snippet = str(request.get("python_snippet", "") or "").strip()
    if snippet:
        try:
            py_result = await code_run("python", snippet, timeout=25)
            chunks.append(f"[Python reasoning tool]\n{py_result[:2200]}")
        except Exception as exc:
            log.debug(f"Deep reasoning python tool skipped: {exc}")

    return "\n\n".join(chunks[:6])


def _update_session_from_frame(
    session_id: str,
    session: dict[str, Any],
    frame: dict[str, Any],
    user_text: str,
) -> dict[str, Any]:
    if frame.get("should_reset_session"):
        session = state_store.clear_scratchpad_session(session_id)
    session["problem_title"] = str(frame.get("problem_title", "") or session.get("problem_title", "")).strip()[:180]
    session["problem_summary"] = str(frame.get("problem_summary", "") or session.get("problem_summary", "")).strip()[:800]
    session["project_brief"] = str(frame.get("project_brief", "") or session.get("project_brief", "")).strip()[:800]
    session["domain"] = str(frame.get("domain", "") or session.get("domain", "general")).strip().lower() or "general"
    session["task_type"] = str(frame.get("task_type", "") or session.get("task_type", "analyze")).strip().lower() or "analyze"
    for key in (
        "goals",
        "learning_goals",
        "constraints",
        "assumptions",
        "unknowns",
        "concepts",
        "open_questions",
    ):
        session[key] = _merge_unique(
            _normalize_list(session.get(key, [])),
            _normalize_list(frame.get(key, [])),
            limit=10,
        )
    history = session.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append({"role": "user", "content": user_text[:800], "ts": time.time()})
    session["history"] = history[-24:]
    session["last_user_turn"] = user_text[:800]
    state_store.upsert_scratchpad_session(session_id, session)
    _emit_workbench(session, stage="framing", status="active")
    return session


def _apply_reasoning_update(
    session_id: str,
    session: dict[str, Any],
    response_data: dict[str, Any],
    reply: str,
) -> dict[str, Any]:
    update = response_data.get("scratchpad_update", {})
    if not isinstance(update, dict):
        update = {}
    for key in (
        "goals",
        "learning_goals",
        "constraints",
        "assumptions",
        "unknowns",
        "evidence",
        "attempted_approaches",
        "dead_ends",
        "decisions",
        "design_decisions",
        "experiments",
        "blockers",
        "concepts",
        "teaching_notes",
        "recommended_tools",
        "open_questions",
        "next_steps",
    ):
        session[key] = _merge_unique(
            _normalize_list(session.get(key, []), limit=12),
            _normalize_list(update.get(key, []), limit=12),
            limit=12,
        )
    summary = str(update.get("problem_summary", "") or "").strip()
    if summary:
        session["problem_summary"] = summary[:800]
    project_brief = str(update.get("project_brief", "") or "").strip()
    if project_brief:
        session["project_brief"] = project_brief[:800]
    history = session.get("history", [])
    if not isinstance(history, list):
        history = []
    history.append({"role": "assistant", "content": reply[:1200], "ts": time.time()})
    session["history"] = history[-24:]
    session["last_assistant_turn"] = reply[:1200]
    state_store.upsert_scratchpad_session(session_id, session)
    _emit_workbench(session, stage="answer", status="active")
    return session


async def _synthesize_reasoning_reply(
    user_text: str,
    context_hint: str,
    session: dict[str, Any],
    mode: dict[str, Any],
    support_material: str,
) -> dict[str, Any]:
    llm = get_client()
    scaffold = _domain_scaffold(session.get("domain", mode.get("domain", "general")), session.get("task_type", mode.get("task_type", "analyze")))
    prompt = f"""Solve this collaboratively using deliberate reasoning.

{scaffold}

Return strict JSON only:
{{
  "answer": "",
  "assumptions_used": [],
  "verification_checks": [],
  "follow_up_question": "",
  "scratchpad_update": {{
    "problem_summary": "",
    "project_brief": "",
    "learning_goals": [],
    "assumptions": [],
    "unknowns": [],
    "evidence": [],
    "attempted_approaches": [],
    "dead_ends": [],
    "decisions": [],
    "design_decisions": [],
    "experiments": [],
    "blockers": [],
    "concepts": [],
    "teaching_notes": [],
    "recommended_tools": [],
    "open_questions": [],
    "next_steps": []
  }}
}}

Answer requirements:
- Be genuinely useful, not generic.
- Reason step by step internally, but present the answer as a sharp collaborator would.
- Teach while solving: if the user would benefit from a concept or mental model, explain it briefly without derailing progress.
- If this is technical work, mention what you would verify, simulate, research, or challenge next when useful.
- If uncertainty remains, say what matters and why.
- If a follow-up would unlock much better reasoning, put one concise question in follow_up_question.
- Do not mention JSON or internal scaffolds.

Persistent scratchpad:
{_scratchpad_text(session)[:2200]}

Recent reasoning history:
{_recent_history_text(session, limit=10)}

Current environment context:
{(context_hint or "None")[:1600]}

Supporting material:
{(support_material or "None")[:3200]}

User turn:
{user_text}
"""
    resp = await llm.create(
        messages=[{"role": "user", "content": prompt}],
        model_type="reasoning",
        max_tokens=900,
    )
    return _parse_json_object(resp.text)


async def _verify_reasoning_reply(
    user_text: str,
    draft_data: dict[str, Any],
    context_hint: str,
    session: dict[str, Any],
) -> dict[str, Any]:
    llm = get_client()
    if llm.provider == "none":
        return {
            "approved": True,
            "issues": [],
            "revised_answer": str(draft_data.get("answer", "") or "").strip(),
        }
    prompt = f"""Verify this assistant reply for correctness, completeness, and usefulness.

Return strict JSON only:
{{
  "approved": true | false,
  "issues": [],
  "revised_answer": ""
}}

Check for:
- Does it address the user's actual question?
- Is the reasoning internally consistent?
- Are units, assumptions, constraints, and tradeoffs treated sanely when relevant?
- Is the answer too vague when it should be concrete?

Scratchpad:
{_scratchpad_text(session)[:1800]}

Context:
{(context_hint or "None")[:1200]}

User turn:
{user_text}

Draft answer:
{str(draft_data.get("answer", "") or "")[:2600]}
"""
    resp = await llm.create(
        messages=[{"role": "user", "content": prompt}],
        model_type="scoring",
        max_tokens=260,
    )
    data = _parse_json_object(resp.text)
    revised = str(data.get("revised_answer", "") or "").strip()
    if not revised:
        revised = str(draft_data.get("answer", "") or "").strip()
    return {
        "approved": bool(data.get("approved", False)),
        "issues": _normalize_list(data.get("issues", []), limit=5),
        "revised_answer": revised,
    }


async def maybe_handle_deep_reasoning(
    user_text: str,
    context_hint: str = "",
    session_id: str = "default",
) -> str | None:
    mode = await decide_reasoning_mode(user_text, context_hint=context_hint, session_id=session_id)
    if mode.get("mode") != "deep":
        return None

    try:
        db.insert_conversation(time.time(), "user", user_text[:1200], "deep_reasoning")
    except Exception:
        pass

    session = state_store.get_scratchpad_session(session_id)
    frame = await _frame_problem(user_text, context_hint, session, mode)
    session = _update_session_from_frame(session_id, session, frame, user_text)

    if frame.get("needs_clarification"):
        question = str(frame.get("clarifying_question", "") or "").strip()
        if question:
            session["verification_status"] = {"status": "clarify", "issues": [], "checks": []}
            state_store.upsert_scratchpad_session(session_id, session)
            _apply_reasoning_update(
                session_id,
                session,
                {"scratchpad_update": {"open_questions": [question]}},
                question,
            )
            _emit_workbench(session, stage="clarify", status="awaiting_input")
            return question

    support = await _gather_supporting_material(user_text, context_hint, session, mode)
    if support:
        _emit_workbench(
            session,
            stage="research",
            status="active",
            extra={"support_material": support[:800]},
        )
    draft = await _synthesize_reasoning_reply(user_text, context_hint, session, mode, support)
    draft_answer = str(draft.get("answer", "") or "").strip()
    if not draft_answer:
        return None

    verdict = await _verify_reasoning_reply(user_text, draft, context_hint, session)
    session["verification_status"] = {
        "status": "approved" if verdict.get("approved") else "revised",
        "issues": verdict.get("issues", []),
        "checks": _normalize_list(draft.get("verification_checks", []), limit=5),
    }
    state_store.upsert_scratchpad_session(session_id, session)
    reply = verdict.get("revised_answer", draft_answer).strip() or draft_answer
    follow_up = str(draft.get("follow_up_question", "") or "").strip()
    if follow_up:
        reply = f"{reply}\n\nQuestion: {follow_up}"

    _apply_reasoning_update(session_id, session, draft, reply)
    try:
        db.insert_conversation(time.time(), "assistant", reply, "deep_reasoning")
    except Exception:
        pass
    _emit_workbench(session, stage="verified", status="complete")
    return reply


def get_scratchpad_summary(session_id: str = "default") -> str:
    session = state_store.get_scratchpad_session(session_id)
    return _scratchpad_text(session)
