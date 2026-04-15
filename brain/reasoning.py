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
from personality.marrow import REASONING_PROMPT, WORLD_MODEL_EXTRACTION_PROMPT
from storage import db
from voice.speak import speak, speak_filler

log = logging.getLogger(__name__)


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
            focused = s.get("focused_context", "")
            chash = s.get("content_hash", "")

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
                entry = f"  {title[:80]}\n  {text[:700]}"
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


def _build_world_model_summary() -> str:
    """
    Build a concise summary of what Marrow knows about the user.
    Groups by type for readability.
    """
    obs = db.get_observations(limit=40)
    if not obs:
        return ""

    by_type: dict = {}
    for o in obs:
        t = o["type"]
        by_type.setdefault(t, []).append(o["content"])

    lines = ["=== WHAT I KNOW ==="]
    for type_, items in by_type.items():
        lines.append(f"\n[{type_.upper()}]")
        for item in items[:6]:
            lines.append(f"  • {item}")

    return "\n".join(lines)


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


async def _run_reasoning(
    context_str: str,
    world_model: str,
    deep_world: str,
) -> Optional[dict]:
    """
    Ask the LLM if there's anything worth saying or doing.
    Returns parsed JSON or None.
    """
    from brain.llm import get_client
    llm = get_client()

    user_content = (
        f"{deep_world}\n\n{world_model}\n\n{context_str}"
        if world_model
        else context_str
    )

    try:
        response = await llm.create(
            messages=[{"role": "user", "content": user_content}],
            system=DEEP_REASONING_PROMPT,
            max_tokens=600,
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


async def _extract_world_model(
    context_str: str,
    screenshots: list,
) -> None:
    """
    Background task: extract durable facts from context into the world model.
    Uses scoring model (fast + cheap) since this runs every cycle.
    """
    from brain.llm import get_client
    llm = get_client()

    try:
        # Update live world model from current screen
        if screenshots:
            latest = screenshots[0]
            update_world_from_screen(
                app=latest.get("app_name", ""),
                title=latest.get("window_title", ""),
                focused=latest.get("focused_context", ""),
                ocr=latest.get("ocr_text", ""),
            )

        # Extract observations via LLM
        response = await llm.create(
            messages=[{
                "role": "user",
                "content": f"{WORLD_MODEL_EXTRACTION_PROMPT}\n\nContext:\n{context_str}",
            }],
            max_tokens=512,
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

    except Exception as e:
        log.debug(f"World model extraction error: {e}")


# ─── Deep reasoning system prompt ───────────────────────────────────────────────

DEEP_REASONING_PROMPT = """You are Marrow — an ambient intelligence watching someone's screen and listening to them in real time.

Your job: decide if there is something worth saying or doing RIGHT NOW based on what you see and hear.

## How to read the context
- SCREEN: recent screenshots (newest first), with app transitions marked
- AUDIO: what the user has said aloud recently
- WORLD STATE: what you know about the user's projects, people, goals

## When to speak
Speak only when you have something genuinely useful:
- They're stuck on something you can solve (error, block, confusion)
- There's a connection between now and something from their past you know about
- They're about to miss something important (deadline, conflict, detail)
- They said something out loud that needs a response or action
- You spotted something they haven't noticed that changes what they should do

## When to stay silent
- Routine work — browsing, reading, normal flow
- Nothing has meaningfully changed since last check
- The insight is obvious or they likely already know it
- You'd just be narrating what they can see themselves

## When to act (without speaking)
- A background task is clearly needed (lookup, draft, summarize)
- They mentioned wanting something done and haven't done it

## Output (JSON — pick ONE pattern)

Speak only:
{"speak": true, "message": "1-3 sentences, direct, no hedging", "reasoning": "why now", "urgency": <number>}

Speak + act:
{"speak": true, "message": "what you're about to do", "reasoning": "why", "urgency": <number>, "act": {"task": "exact task", "context": "relevant context"}}

Act silently:
{"speak": false, "act": {"task": "task", "context": "context"}, "urgency": <number>}

Nothing:
{"speak": false}

## Urgency scale (IMPORTANT — use these exact meanings)
5 = CRITICAL — time-sensitive emergency, say it no matter what
4 = HIGH — clearly important, interrupt even in meetings
3 = MEDIUM — worth saying when cooldown allows
2 = LOW — say it only if they seem free
1 = SKIP — not worth interrupting for

## Rules
- Be ruthless about saying nothing. Most moments don't need commentary.
- Never narrate what they can already see.
- Never be generic ("looks like you're working hard"). Be specific to exactly what's on screen.
- Shorter is better. One sharp sentence beats three hedged ones."""


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

    while True:
        cycle_start = time.time()

        try:
            context = db.get_recent_context(config.CONTEXT_WINDOW_SECONDS)
            context_str = _build_context_summary(context)
            world_model = _build_world_model_summary()
            deep_world = _build_deep_world_context()

            log.debug("Running reasoning cycle...")

            # Reasoning + world model extraction run in parallel
            result, _ = await asyncio.gather(
                _run_reasoning(context_str, world_model, deep_world),
                _extract_world_model(context_str, context.get("screenshots", [])),
                return_exceptions=False,
            )

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
        if interrupt_engine.should_speak(candidate):
            interrupt_engine.record_spoken(candidate)

            if act:
                # Speak the message (tells user what we're doing), then act
                await speak(message)
                await _run_action(act, context_str)
            else:
                await speak(message)
        else:
            log.debug(f"Candidate suppressed: {message[:60]}")

    elif act and not should_speak:
        # Silent action — do the work without speaking
        # Only run if urgency is high enough to act without prompting
        if urgency >= 3:
            log.info(f"Silent action (urgency {urgency}): {act.get('task', '')[:60]}")
            await speak_filler()  # brief acknowledgment
            await _run_action(act, context_str)
        else:
            log.debug(f"Silent action suppressed: urgency too low ({urgency})")

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
