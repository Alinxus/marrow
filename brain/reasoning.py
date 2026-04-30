"""
Proactive Reasoning Loop — the core of Marrow.

Every REASONING_INTERVAL seconds:
  1. Pull recent screen + audio context from DB
  2. Build context string (with app transitions, not just latest state)
  3. Run reasoning + world model extraction in parallel
  4. Parse result: may contain speak=true, act={task}, or both
  5. If speak: pass to InterruptDecisionEngine → speak() if approved
  6. If act: call execute_action() [optionally after speaking a filler]
  7. World model extraction runs in background regardless

Key improvements over v1:
  - Action extraction: reasoning can now return act={} to trigger executor
  - App transitions: context shows what apps the user has moved through,
    not just the latest screenshot
  - Filler before action: if acting, speak a filler immediately so user
    hears something before the async executor runs
  - Emotional hints: world model summary includes behavioral patterns
  - Deep reasoning: Reflection, planning, self-correction, meta-reasoning
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Optional

import config
from brain.interrupt import InterruptCandidate, InterruptDecisionEngine
from brain.world_model import (
    get_world_model,
    get_world_context,
    update_world_from_screen,
)
from brain.context_awareness import build_high_signal_context
from personality.marrow import WORLD_MODEL_EXTRACTION_PROMPT
from storage import db
from voice.speak import speak, speak_filler

log = logging.getLogger(__name__)


def _push_to_swift(message: str, urgency: float = 3) -> None:
    """Push a proactive notification to connected Swift FloatingControlBar clients."""
    try:
        from server import push_proactive_event
        push_proactive_event(
            title="Marrow",
            message=message,
            assistant_id="marrow-reasoning",
            context={"urgency": urgency},
        )
    except Exception:
        pass  # server may not be running (Windows/headless without server)

_last_reasoning_context_hash: str = ""
_last_reasoning_attempt_ts: float = 0.0


# ─── Context building ──────────────────────────────────────────────────────────


def _build_context_summary(context: dict) -> str:
    """
    Format recent screen + audio into a readable block.
    Shows app transitions (what changed) not just the latest state.
    """
    parts = []
    screenshots = context["screenshots"]
    transcripts = context["transcripts"]

    if screenshots:
        parts.append("=== SCREEN (recent, newest first) ===")
        seen_apps = []
        seen_hashes = set()
        for s in screenshots[:15]:
            app = s.get("app_name") or "unknown"
            title = s.get("window_title") or ""
            text = (s.get("ocr_text") or "").strip()
            raw_ocr = (s.get("ocr_raw_text") or "").strip()
            vision_text = (s.get("vision_text") or "").strip()
            payload_json = (s.get("screen_payload_json") or "").strip()
            focused = s.get("focused_context", "")
            chash = s.get("content_hash", "")
            payload = {}
            if payload_json:
                try:
                    payload = json.loads(payload_json)
                except Exception:
                    payload = {}

            # Skip if we already included this exact screen content
            if chash and chash in seen_hashes:
                continue
            if chash:
                seen_hashes.add(chash)

            # Mark app transitions
            if not seen_apps or app != seen_apps[-1]:
                seen_apps.append(app)
                parts.append(f"\n[{app}]")

            if text:
                entry_parts = [f"  {title[:80]}"]
                if payload.get("metadata"):
                    meta = payload.get("metadata", {})
                    if meta.get("url"):
                        entry_parts.append(f"  URL: {str(meta.get('url'))[:220]}")
                if vision_text:
                    entry_parts.append(f"  Vision: {vision_text[:700]}")
                elif text:
                    entry_parts.append(f"  Summary: {text[:700]}")
                if raw_ocr:
                    entry_parts.append(f"  OCR: {raw_ocr[:900]}")
                entry = "\n".join(entry_parts)
                parts.append(entry)
            elif title:
                parts.append(f"  {title[:80]}")

            if focused:
                parts.append(f"  → {focused}")

    if transcripts:
        parts.append("\n=== AUDIO ===")
        # Combine into a flowing transcript
        combined = " ".join(t["text"] for t in transcripts)
        parts.append(combined[:800])

    return "\n".join(parts) if parts else "No context captured yet."


async def _build_semantic_memory_context(current_context: str) -> str:
    """
    Best-quality memory context for the reasoning loop.

    Layer 1: Personal wiki (fast, local, structured)
    Layer 2: Oracle search (semantic + lexical + phonetic + temporal + graph)
             — or semantic search as fallback
    Layer 3: Memory graph connections (relationships RetainDB found)
    Layer 4: Gap checking (fire-and-forget, doesn't block context assembly)
    Layer 5: Local observation fallback
    """
    from brain.wiki import wiki_context
    from brain.agi import get_agi
    from actions.memory import get_memory_client

    parts = []
    agi = get_agi()

    # 1. Personal wiki — structured, fast
    wiki = wiki_context()
    if wiki:
        parts.append(wiki)

    # 2. Oracle memory search — best retrieval quality
    if current_context:
        oracle_ctx = await agi.get_oracle_context(current_context[:600], limit=8)
        if oracle_ctx:
            parts.append(oracle_ctx)

    # 2.5 RetainDB context/profile (throttled + cached, stronger awareness)
    global _last_retaindb_ctx_fetch, _last_retaindb_ctx_key, _last_retaindb_ctx_value
    global _last_retaindb_profile_fetch, _last_retaindb_profile_value
    client = get_memory_client()
    now = time.time()
    if client and current_context:
        query_key = hashlib.md5(current_context[:900].encode("utf-8")).hexdigest()
        need_ctx_fetch = query_key != _last_retaindb_ctx_key or (
            now - _last_retaindb_ctx_fetch
        ) >= max(20, int(getattr(config, "RETAINDB_CONTEXT_REFRESH_SECONDS", 75)))
        if need_ctx_fetch:
            try:
                remote_ctx = await client.query_context(
                    current_context[:900], include_profile=True
                )
                assembled = (
                    remote_ctx.get("context") or remote_ctx.get("content") or ""
                ).strip()
                if assembled:
                    _last_retaindb_ctx_value = assembled[:2200]
                    _last_retaindb_ctx_key = query_key
                    _last_retaindb_ctx_fetch = now
            except Exception as e:
                log.debug(f"RetainDB context query failed: {e}")
        if _last_retaindb_ctx_value:
            parts.append(f"=== RETAINDB CONTEXT ===\n{_last_retaindb_ctx_value}")

        need_profile_fetch = (now - _last_retaindb_profile_fetch) >= max(
            60, int(getattr(config, "RETAINDB_PROFILE_REFRESH_SECONDS", 300))
        )
        if need_profile_fetch:
            try:
                profile = await client.get_profile_model()
                if profile and not profile.get("error"):
                    compact = {
                        "preferences": profile.get("preferences", {}),
                        "goals": profile.get("goals", []),
                        "working_style": profile.get("working_style", ""),
                        "frequent_entities": profile.get("frequent_entities", [])[:10],
                    }
                    _last_retaindb_profile_value = json.dumps(compact)[:1400]
                    _last_retaindb_profile_fetch = now
            except Exception as e:
                log.debug(f"RetainDB profile fetch failed: {e}")
        if _last_retaindb_profile_value:
            parts.append(f"=== RETAINDB PROFILE ===\n{_last_retaindb_profile_value}")

    # 3. Memory graph connections
    graph_ctx = agi.get_graph_context()
    if graph_ctx:
        parts.append(graph_ctx)

    # 4. Check if context answers open gap questions (fire-and-forget)
    if current_context:
        asyncio.create_task(agi.check_gaps_against_context(current_context))

    # 5. Local fallback if cloud empty
    if len(parts) <= 1:
        obs = db.get_observations(limit=30)
        if obs:
            by_type: dict = {}
            for o in obs:
                by_type.setdefault(o["type"], []).append(o["content"])
            lines = ["=== RECENT OBSERVATIONS ==="]
            for type_, items in by_type.items():
                lines.append(f"[{type_.upper()}]")
                for item in items[:4]:
                    lines.append(f"  • {item}")
            parts.append("\n".join(lines))

    return "\n\n".join(parts) if parts else ""


def _build_deep_world_context() -> str:
    """Build the deep world model context for impressive reasoning."""
    world = get_world_model()

    lines = ["=== WORLD STATE ==="]

    # Current focus
    if world.current_focus:
        lines.append(f"**Currently:** {world.current_focus}")

    # Active entities
    active = [
        e
        for e in world.entities.values()
        if time.time() - e.last_seen < 300  # Last 5 minutes
    ]

    if active:
        by_type = {}
        for e in active:
            by_type.setdefault(e.entity_type, []).append(e.name)

        for etype, names in by_type.items():
            lines.append(f"**{etype}s:** {', '.join(names[:5])}")

    # Recent topics
    if world.topics:
        top = sorted(world.topics.items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append(f"**Hot topics:** {', '.join([t[0] for t in top])}")

    # Recent events
    if world.recent_events:
        lines.append("**Recent events:**")
        for ev in world.recent_events[-3:]:
            lines.append(f"  - {ev['content'][:80]}")

    return "\n".join(lines)


# ─── Claude calls ──────────────────────────────────────────────────────────────


async def _run_reasoning(full_context: str) -> Optional[dict]:
    """
    Ask the LLM if there's anything worth saying or doing.
    full_context already contains world state + memory + screen/audio.
    Returns parsed JSON or None.
    """
    from brain.llm import get_client

    llm = get_client()

    user_content = full_context[: config.REASONING_CONTEXT_CHAR_LIMIT]

    try:
        t0 = time.time()
        response = await llm.create(
            messages=[{"role": "user", "content": user_content}],
            system=DEEP_REASONING_PROMPT,
            max_tokens=config.REASONING_MAX_TOKENS,
            model_type="reasoning",
        )
        raw = response.text.strip()

        # Extract first JSON object
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            log.debug(f"No JSON found in reasoning: {raw[:200]}")
            return None

        return json.loads(raw[start:end])

    except json.JSONDecodeError as e:
        log.debug(f"Reasoning JSON parse error: {e} | raw: {raw[:100]}")
        return None
    except Exception as e:
        log.error(f"Reasoning error: {e}")
        return None


# Cache: skip world model LLM extraction when context hasn't changed
_last_world_extract_hash: str = ""

# RetainDB context/profile caches
_last_retaindb_ctx_fetch: float = 0.0
_last_retaindb_ctx_key: str = ""
_last_retaindb_ctx_value: str = ""
_last_retaindb_profile_fetch: float = 0.0
_last_retaindb_profile_value: str = ""


async def _extract_world_model(
    context_str: str,
    screenshots: list,
) -> None:
    """
    Background task: extract durable facts from context into the world model.
    Uses scoring model (fast + cheap) since this runs every cycle.
    Skips the LLM call if context hasn't changed since last extraction.
    """
    global _last_world_extract_hash

    from brain.llm import get_client

    llm = get_client()

    try:
        # Always update live world model from current screen (no LLM, cheap)
        if screenshots:
            latest = screenshots[0]
            update_world_from_screen(
                app=latest.get("app_name", ""),
                title=latest.get("window_title", ""),
                focused=latest.get("focused_context", ""),
                ocr=latest.get("ocr_text", ""),
            )

        # Skip LLM extraction if context unchanged since last cycle
        ctx_hash = hashlib.md5(context_str.encode(), usedforsecurity=False).hexdigest()
        if ctx_hash == _last_world_extract_hash:
            log.debug("World model: context unchanged, skipping LLM extraction")
            return
        _last_world_extract_hash = ctx_hash

        # Extract observations via LLM
        response = await llm.create(
            messages=[
                {
                    "role": "user",
                    "content": f"{WORLD_MODEL_EXTRACTION_PROMPT}\n\nContext:\n{context_str[:2200]}",
                }
            ],
            max_tokens=config.WORLD_MODEL_MAX_TOKENS,
            model_type="scoring",
        )
        raw = response.text.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return

        observations = json.loads(raw[start:end])
        new_count = 0
        for obs in observations:
            if "type" in obs and "content" in obs and obs["content"].strip():
                inserted = db.insert_observation(obs["type"], obs["content"])
                if inserted:
                    new_count += 1
                    log.debug(f"World model +[{obs['type']}]: {obs['content'][:80]}")
        if new_count:
            log.debug(f"World model: {new_count} new observations")
            # Nudge wiki to pick up new observations on next cycle
            try:
                from brain.wiki import get_wiki

                get_wiki()._last_update = 0  # force refresh next cycle
            except Exception:
                pass

    except Exception as e:
        log.debug(f"World model extraction error: {e}")


# ─── Deep reasoning system prompt ───────────────────────────────────────────────

DEEP_REASONING_PROMPT = """You are Marrow — an ambient intelligence watching someone's screen and listening to them in real time.

Your job: decide if there is something worth saying or doing RIGHT NOW.

Runtime truth:
- Marrow runs continuously by default (not turn-based only).
- Never claim "I only watch when asked" unless context explicitly indicates disabled or stale capture.

## Reading the context
- SCREEN: recent screenshots, newest first, with app transitions
- AUDIO: what the user has said or heard aloud recently
- WORLD STATE: user's projects, people, goals, long-term patterns
- LONG-HORIZON CONTEXT: verified claims, communication pressure, meeting shifts

## When to speak — what each situation demands

**Misinformation / false claims in media** (urgency 4)
Something on screen or in audio contains a claim verified as false or misleading.
State the fact, not your uncertainty. Name the source: "That's false — Epstein died in 2019, confirmed by the NYC medical examiner."
Don't hedge. If it's been verified, own it.

**Communication pressure / ignored outreach** (urgency 3-4)
User is drafting or sending another message to someone who hasn't replied to prior messages.
Say it directly: "You've sent this person 3 messages with no reply. Worth reconsidering before sending another?"
If drafting an apology to someone who's been ignoring them: name it.

**Video call social signals** (urgency 3-4)
New face or participant appeared in a video call. State the observation plainly.
Don't over-interpret — just surface it: "Someone joined the call behind her."

**Stuck / blocked** (urgency 3)
Visible error, repeated failed attempt, clear block.
Offer the fix, not a suggestion: "The issue is X, here's the fix."

**Decision / trade-off help** (urgency 3-4)
If the user is clearly choosing between options, deciding what to do next, or circling around a task,
have an opinion. Be a sharp friend, not a neutral summarizer.
Say what you think they should do and why in plain language: "Pick option B. It's cheaper, reversible, and good enough for today."

**Momentum / stalled execution** (urgency 2-3)
If the user has a concrete task in front of them but no clear next move, give the next move.
Don't ask vague coaching questions. Reduce uncertainty: "Reply to Alex first, then merge the smaller PR."

**Non-obvious connection from memory** (urgency 2-3)
Something from the user's past directly applies to right now.
Surface the connection: "You hit this same issue in March — the fix was Y."

**Background task clearly needed** (urgency 2)
Something useful can be done silently, or they mentioned wanting it.
Act without speaking unless the result matters immediately.

## When to stay silent
- Routine work — browsing, reading, normal flow
- Nothing changed meaningfully since last check
- The insight is obvious or they already know it
- You'd just be narrating what they can already see

## Output (JSON — pick ONE)

Speak only:
{"speak": true, "message": "1-3 sentences, direct", "reasoning": "why now", "urgency": <number>}

Speak + act:
{"speak": true, "message": "what you're about to do", "reasoning": "why", "urgency": <number>, "act": {"task": "exact task", "context": "relevant context"}}

Act silently:
{"speak": false, "act": {"task": "task", "context": "context"}, "urgency": <number>}

Nothing:
{"speak": false}

## Urgency scale
5 = CRITICAL — say it regardless of anything
4 = HIGH — interrupt even in meetings
3 = MEDIUM — say it when cooldown allows
2 = LOW — only if they seem free
1 = SKIP

## Rules
- Default to silence for low-value chatter, but brief grounded check-ins are allowed when context meaningfully changes.
- Never narrate what they can already see.
- Never be generic. Be specific to exactly what's on screen right now.
- For decisions, trade-offs, and ambiguity: have a grounded opinion instead of staying neutral.
- Never say "signals", "models", "pipeline", "heuristics" to the user.
- For verified claims: state the fact, not the uncertainty.
- Prefer concise language, but allow up to 3-4 sentences when that makes the guidance clearer."""


_FREQUENCY_TO_GATE_THRESHOLD = {
    1: 0.92,
    2: 0.85,
    3: 0.72,
    4: 0.62,
    5: 0.54,
}

_GATE_PROMPT = """You are the proactive notification gate.

Decide if this moment is worth interrupting the user at all.

IMPORTANT: Most moments are NOT worth an interruption. Default to should_notify=false.

Exception: when a short, concrete ambient update would reduce uncertainty or keep momentum,
you may allow it with moderate relevance.

Return strict JSON only:
{"should_notify": true|false, "relevance_score": 0.0-1.0, "why": "one short sentence"}

Rules:
- High score only when interruption changes the user's next action now.
- should_notify=true only if there is a concrete mistake risk, time-critical opportunity,
  or a genuinely non-obvious connection the user would likely miss.
- Reject routine browsing, obvious reminders, generic coaching, or weakly grounded hunches.
- Reject anything repetitive or similar to recent interruptions.
- Be conservative and specific.
"""


_CRITIC_PROMPT = """You are the final critic for a proactive interruption.

Evaluate whether this message should be sent now.

Message: {message}
Reasoning: {reasoning}
Context: {context}

Return strict JSON only:
{{"approved": true|false, "confidence": 0.0-1.0, "why": "one short sentence"}}

Imagine the user is busy and sees this interrupt.
Approve only if they'd think: "glad I saw this, this changes what I do next."

Reject if ANY are true:
- Generic / obvious / repetitive
- Vague corporate wording
- Weak grounding in current context
- Bad timing or low urgency
- Removing this interruption would change nothing

Approve only if ALL are true:
- Specific and concrete to the exact moment
- Non-obvious enough that user may miss it
- Actionable now
"""


def _gate_threshold() -> float:
    freq = max(1, min(5, int(config.PROACTIVE_FREQUENCY)))
    return _FREQUENCY_TO_GATE_THRESHOLD.get(freq, 0.74)


def _daily_limit_ok() -> bool:
    # Keep only a hard emergency brake for runaway loops.
    # Day-to-day interrupt volume should be model/context-driven.
    hard_safety_limit = 120
    cutoff = time.time() - 86400
    today_interrupts = db.count_interruptions_since(cutoff)
    if today_interrupts >= hard_safety_limit:
        log.warning(
            f"Interrupt safety brake hit ({today_interrupts}/{hard_safety_limit})"
        )
        return False
    return True


async def _gate_context(full_context: str) -> bool:
    """Omi-style gate pass before expensive reasoning/action stage."""
    from brain.llm import get_client

    llm = get_client()
    if llm.provider == "none":
        db.insert_proactive_decision(
            lane="reasoning", stage="gate", status="skip", reason="no_llm"
        )
        return False

    try:
        t0 = time.time()
        response = await llm.create(
            messages=[
                {
                    "role": "user",
                    "content": full_context[: config.GATE_CONTEXT_CHAR_LIMIT],
                }
            ],
            system=_GATE_PROMPT,
            max_tokens=config.GATE_MAX_TOKENS,
            model_type="scoring",
        )
        raw = response.text.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            db.insert_proactive_decision(
                lane="reasoning", stage="gate", status="pass", reason="non_json"
            )
            return True

        gate = json.loads(raw[start:end])
        score = float(gate.get("relevance_score", 0.5))
        should_notify = bool(gate.get("should_notify", False))
        threshold = _gate_threshold()
        passed = should_notify and score >= threshold
        gate_ms = (time.time() - t0) * 1000.0
        log.debug(f"Gate: score={score:.2f} threshold={threshold:.2f} pass={passed}")
        db.insert_proactive_decision(
            lane="reasoning",
            stage="gate",
            status="pass" if passed else "reject",
            score=score,
            latency_ms=gate_ms,
            reason=(gate.get("reasoning") or "")[:180],
        )
        return passed
    except Exception as e:
        log.debug(f"Gate fallback (error): {e}")
        db.insert_proactive_decision(
            lane="reasoning",
            stage="gate",
            status="pass",
            reason=f"error_fallback:{e}"[:180],
        )
        return True


async def _critic_approve(message: str, reasoning: str, context: str) -> bool:
    """Final critic check to suppress weak interrupts."""
    from brain.llm import get_client

    llm = get_client()
    if llm.provider == "none":
        db.insert_proactive_decision(
            lane="reasoning", stage="critic", status="skip", reason="no_llm"
        )
        return False

    prompt = _CRITIC_PROMPT.format(
        message=message[:300],
        reasoning=reasoning[:200],
        context=context[:700],
    )
    try:
        t0 = time.time()
        response = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=config.CRITIC_MAX_TOKENS,
            model_type="scoring",
        )
        raw = response.text.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            db.insert_proactive_decision(
                lane="reasoning", stage="critic", status="pass", reason="non_json"
            )
            return True

        critic = json.loads(raw[start:end])
        approved = bool(critic.get("approved", False))
        confidence = float(critic.get("confidence", 0.5))
        ok = approved and confidence >= 0.45
        critic_ms = (time.time() - t0) * 1000.0
        log.debug(f"Critic: approved={approved} confidence={confidence:.2f} pass={ok}")
        db.insert_proactive_decision(
            lane="reasoning",
            stage="critic",
            status="pass" if ok else "reject",
            score=confidence,
            latency_ms=critic_ms,
            reason=(critic.get("reasoning") or "")[:180],
            payload=message[:200],
        )
        return ok
    except Exception as e:
        log.debug(f"Critic fallback (error): {e}")
        db.insert_proactive_decision(
            lane="reasoning",
            stage="critic",
            status="pass",
            reason=f"error_fallback:{e}"[:180],
        )
        return True


# ─── Main loop ─────────────────────────────────────────────────────────────────


async def reasoning_loop(
    interrupt_engine: InterruptDecisionEngine,
) -> None:
    """
    Main proactive reasoning loop. Runs forever.
    Waits one full interval before first run so there's context to work with.
    """
    log.info(f"Reasoning loop started (interval: {config.REASONING_INTERVAL}s)")
    await asyncio.sleep(config.REASONING_INTERVAL)

    global _last_reasoning_attempt_ts, _last_reasoning_context_hash

    cycle_index = 0
    cached_memory_context = ""

    while True:
        cycle_start = time.time()
        cycle_index += 1

        try:
            context = db.get_recent_context(config.CONTEXT_WINDOW_SECONDS)
            context_str = _build_context_summary(context)
            deep_world = _build_deep_world_context()

            log.debug("Running reasoning cycle...")

            # Semantic memory context is expensive. Refresh every N cycles.
            refresh_n = max(1, int(config.MEMORY_REFRESH_CYCLES))
            if (cycle_index % refresh_n == 1) or not cached_memory_context:
                cached_memory_context = await _build_semantic_memory_context(
                    context_str
                )
            memory_context = cached_memory_context

            high_signal_context = build_high_signal_context()

            # Proactive context: calendar, focus state, distraction, EOD
            proactive_context = ""
            try:
                from brain.proactive import get_proactive_context

                proactive_context = get_proactive_context()
            except Exception:
                pass

            # Assemble full context for reasoning
            full_context = "\n\n".join(
                filter(
                    None,
                    [
                        deep_world,
                        proactive_context,
                        high_signal_context,
                        memory_context,
                        context_str,
                    ],
                )
            )

            context_hash = hashlib.md5(
                full_context[: config.REASONING_CONTEXT_CHAR_LIMIT].encode("utf-8")
            ).hexdigest()
            refresh_after = max(config.REASONING_INTERVAL * 4, 90)
            if (
                context_hash == _last_reasoning_context_hash
                and (time.time() - _last_reasoning_attempt_ts) < refresh_after
            ):
                log.debug("Reasoning skipped: context unchanged")
                elapsed = time.time() - cycle_start
                await asyncio.sleep(max(0.0, config.REASONING_INTERVAL - elapsed))
                continue
            _last_reasoning_context_hash = context_hash
            _last_reasoning_attempt_ts = time.time()

            # Gate first (Omi-style): skip interruption generation on low-value moments
            gate_passed = await _gate_context(full_context)

            if gate_passed:
                # Reasoning + world model extraction run in parallel
                result, _ = await asyncio.gather(
                    _run_reasoning(full_context),
                    _extract_world_model(context_str, context.get("screenshots", [])),
                    return_exceptions=False,
                )
            else:
                await _extract_world_model(context_str, context.get("screenshots", []))
                result = None
                log.debug("Gate rejected moment: no proactive output")

            if not result:
                log.debug("Reasoning: nothing to surface")
            else:
                await _handle_result(result, context_str, interrupt_engine)

        except Exception as e:
            log.error(f"Reasoning loop error: {e}", exc_info=True)

        # Sleep for remaining interval (reasoning call takes some time)
        elapsed = time.time() - cycle_start
        sleep_for = max(0.0, config.REASONING_INTERVAL - elapsed)
        await asyncio.sleep(sleep_for)


def get_retaindb_context_stats() -> dict:
    return {
        "last_ctx_fetch": float(_last_retaindb_ctx_fetch),
        "ctx_cached": bool(_last_retaindb_ctx_value),
        "ctx_chars": len(_last_retaindb_ctx_value or ""),
        "last_profile_fetch": float(_last_retaindb_profile_fetch),
        "profile_cached": bool(_last_retaindb_profile_value),
        "profile_chars": len(_last_retaindb_profile_value or ""),
    }


_FOUR_AXIS_PROMPT = """\
Evaluate this proposed AI insight before it interrupts the user.

Insight: "{message}"
Reasoning: "{reasoning}"
Context: {context}

Score each axis 0.0-1.0:
- actionability: Does this enable a concrete action the user can take NOW?
- timeliness: Is the timing genuinely important — would it be less useful later?
- non_obviousness: Would the user figure this out themselves in the next 30 seconds?
- specificity: Is this grounded in specific facts from their context (not generic advice)?

Anti-patterns that force score=0 overall (return immediately):
- Generic wellness: "take a break", "stay hydrated", "you got this"
- Motivational platitudes without specifics
- Narrating what they can already see on screen
- Hedged language: "it seems like", "you might want to", "perhaps consider"
- Restating what the user just said or did

Return JSON only:
{"actionability": 0.0, "timeliness": 0.0, "non_obviousness": 0.0, "specificity": 0.0, "veto": false, "veto_reason": ""}

veto=true means instant rejection regardless of scores."""

_ANTI_PATTERNS = [
    "take a break",
    "stay hydrated",
    "you got this",
    "great job",
    "keep up the good work",
    "you're doing great",
    "don't forget to",
    "it seems like you",
    "you might want to",
    "perhaps consider",
    "it looks like you",
    "i notice that you",
    "i can see that",
]

_BANNED_STARTS = [
    "confirm",
    "ensure",
    "clarify",
    "consider",
    "prioritize",
    "remember",
    "review",
    "align",
    "make sure",
    "don't forget",
]


async def _four_axis_score(message: str, reasoning: str, context: str) -> float:
    """
    OMI-style 4-axis confidence scoring.
    Returns a composite score 0.0-1.0. Below 0.55 = rejected.
    Returns -1.0 on veto (hard rejection).
    """
    # Fast pre-filter: anti-pattern string match
    msg_lower = message.lower()
    for pattern in _ANTI_PATTERNS:
        if pattern in msg_lower:
            log.debug(f"4-axis: anti-pattern match '{pattern}' — rejected")
            return -1.0

    msg_start = msg_lower.strip().lstrip("\"'`([{").strip()
    for starter in _BANNED_STARTS:
        if msg_start.startswith(starter):
            log.debug(f"4-axis: banned opener '{starter}' — rejected")
            return -1.0

    try:
        from brain.llm import get_client

        llm = get_client()

        prompt = _FOUR_AXIS_PROMPT.format(
            message=message[:300],
            reasoning=reasoning[:200],
            context=context[:400],
        )

        response = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=config.FOUR_AXIS_MAX_TOKENS,
            model_type="scoring",
        )
        raw = response.text.strip()

        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return 0.5  # default: let through if can't score

        scores = json.loads(raw[start:end])

        if scores.get("veto"):
            log.debug(f"4-axis veto: {scores.get('veto_reason', '')}")
            return -1.0

        a = float(scores.get("actionability", 0.5))
        t = float(scores.get("timeliness", 0.5))
        n = float(scores.get("non_obviousness", 0.5))
        s = float(scores.get("specificity", 0.5))

        # Weighted composite — specificity and non-obviousness weighted higher
        composite = (a * 0.2) + (t * 0.2) + (n * 0.3) + (s * 0.3)
        log.debug(f"4-axis: A={a:.2f} T={t:.2f} N={n:.2f} S={s:.2f} → {composite:.2f}")
        return composite

    except Exception as e:
        log.debug(f"4-axis scoring error: {e}")
        return 0.5  # default: let through on error


async def _handle_result(
    result: dict,
    context_str: str,
    interrupt_engine: InterruptDecisionEngine,
) -> None:
    """Process a reasoning result: speak and/or act."""
    should_speak = result.get("speak", False)
    message = (result.get("message") or "").strip()
    reasoning = result.get("reasoning") or ""
    urgency = max(1, min(5, int(result.get("urgency", 3))))
    act = result.get("act")  # {"task": "...", "context": "..."} or None

    # Build candidate (even if we might not speak, for act-only paths)
    candidate = InterruptCandidate(
        message=message,
        reasoning=reasoning,
        urgency=urgency,
        act=act,
    )

    if should_speak and message:
        if not _daily_limit_ok():
            db.insert_proactive_decision(
                lane="reasoning",
                stage="interrupt",
                status="skip",
                reason="daily_safety_brake",
                payload=message[:200],
            )
            return

        # 4-axis confidence filter before even hitting the interrupt engine
        score = await _four_axis_score(message, reasoning, context_str[:400])
        if score < 0:
            log.debug(f"4-axis veto: {message[:60]}")
            db.insert_proactive_decision(
                lane="reasoning",
                stage="four_axis",
                status="reject",
                score=score,
                reason="veto",
                payload=message[:200],
            )
            return
        if score < 0.35:
            log.debug(f"4-axis rejected (score={score:.2f}): {message[:60]}")
            db.insert_proactive_decision(
                lane="reasoning",
                stage="four_axis",
                status="reject",
                score=score,
                reason="below_threshold",
                payload=message[:200],
            )
            return
        db.insert_proactive_decision(
            lane="reasoning",
            stage="four_axis",
            status="pass",
            score=score,
            payload=message[:200],
        )

        # Final critic pass (Omi-style generate -> critic)
        if not await _critic_approve(message, reasoning, context_str):
            log.debug(f"Critic rejected: {message[:60]}")
            return

        if interrupt_engine.should_speak(candidate):
            interrupt_engine.record_spoken(candidate)
            db.insert_proactive_decision(
                lane="reasoning",
                stage="interrupt",
                status="sent",
                score=float(urgency),
                payload=message[:220],
            )
            # Push to Swift UI via proactive WebSocket stream
            _push_to_swift(message, urgency)

            if act:
                await speak(message)
                await _run_action(act, context_str)
            else:
                await speak(message)
        else:
            log.debug(f"Candidate suppressed: {message[:60]}")
            db.insert_proactive_decision(
                lane="reasoning",
                stage="interrupt",
                status="suppress",
                score=float(urgency),
                reason="interrupt_engine",
                payload=message[:220],
            )

    elif message and urgency >= int(
        getattr(config, "PROACTIVE_AUTO_SPEAK_MIN_URGENCY", 2)
    ):
        # Talkative mode: if model produced a concrete message but didn't mark speak,
        # still surface it when interruption policy allows.
        if interrupt_engine.should_speak(candidate):
            interrupt_engine.record_spoken(candidate)
            db.insert_proactive_decision(
                lane="reasoning",
                stage="interrupt_auto",
                status="sent",
                score=float(urgency),
                payload=message[:220],
            )
            await speak(message)
        else:
            log.debug(f"Auto-speak suppressed: {message[:60]}")
            db.insert_proactive_decision(
                lane="reasoning",
                stage="interrupt_auto",
                status="suppress",
                score=float(urgency),
                reason="interrupt_engine",
                payload=message[:220],
            )

    elif act and not should_speak:
        # Silent action — do the work without speaking
        # Only run if urgency is high enough to act without prompting
        if urgency >= 3:
            log.info(f"Silent action (urgency {urgency}): {act.get('task', '')[:60]}")
            db.insert_proactive_decision(
                lane="reasoning",
                stage="action",
                status="run",
                score=float(urgency),
                payload=(act.get("task", "") or "")[:220],
            )
            await speak_filler()  # brief acknowledgment
            await _run_action(act, context_str)
        else:
            log.debug(f"Silent action suppressed: urgency too low ({urgency})")
            db.insert_proactive_decision(
                lane="reasoning",
                stage="action",
                status="suppress",
                score=float(urgency),
                reason="low_urgency",
                payload=(act.get("task", "") or "")[:220],
            )

    else:
        log.debug("Reasoning: nothing to surface")


async def _run_action(act: dict, context_str: str) -> None:
    """Dispatch to action executor."""
    from actions.executor import execute_action

    task = act.get("task", "")
    extra_context = act.get("context", "")
    full_context = (
        f"{extra_context}\n\nRecent context:\n{context_str}"
        if extra_context
        else context_str
    )

    if not task:
        return

    try:
        summary = await execute_action(task, context=full_context)
        log.info(f"Action complete: {summary[:100]}")
    except Exception as e:
        log.error(f"Action failed: {e}")
