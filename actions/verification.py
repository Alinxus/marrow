"""Workspace checkpoints, before/after verification, and rollback hints."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from brain.digital_twin import get_active_workspace_summary
from storage import db

CHECKPOINT_FILE = Path.home() / ".marrow" / "checkpoints.json"


def _compact(text: str, limit: int = 220) -> str:
    return " ".join((text or "").split())[:limit]


def _load() -> dict[str, Any]:
    if not CHECKPOINT_FILE.exists():
        return {"checkpoints": {}}
    try:
        data = json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("checkpoints", {})
            return data
    except Exception:
        pass
    return {"checkpoints": {}}


def _save(data: dict[str, Any]) -> None:
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _recent_screen_snapshot() -> dict[str, Any]:
    ctx = db.get_recent_context(10 * 60)
    shots = ctx.get("screenshots", [])
    latest = shots[0] if shots else {}
    return {
        "app": str(latest.get("app_name", "") or "").strip(),
        "title": str(latest.get("window_title", "") or "").strip(),
        "focused": _compact(str(latest.get("focused_context", "") or ""), 180),
        "ocr": _compact(str(latest.get("ocr_text", "") or ""), 260),
    }


def capture_checkpoint(label: str, expectation: str = "") -> dict[str, Any]:
    snap = {
        "label": label,
        "ts": time.time(),
        "expectation": expectation[:220],
        "workspace": get_active_workspace_summary()[:1200],
        "screen": _recent_screen_snapshot(),
    }
    data = _load()
    checkpoints = data.setdefault("checkpoints", {})
    checkpoints[label] = snap
    _save(data)
    return snap


def get_checkpoint(label: str) -> dict[str, Any]:
    return (_load().get("checkpoints", {}) or {}).get(label, {})


def summarize_checkpoint(label: str) -> str:
    snap = get_checkpoint(label)
    if not snap:
        return f"[error] No checkpoint found for {label}"
    screen = snap.get("screen", {}) or {}
    return "\n".join(
        [
            f"## Checkpoint {label}",
            f"Expectation: {snap.get('expectation', '')}",
            f"Workspace: {str(snap.get('workspace', '') or '')[:1200]}",
            f"Screen: {' | '.join(x for x in [screen.get('app', ''), screen.get('title', ''), screen.get('focused', ''), screen.get('ocr', '')] if x)[:1200]}",
        ]
    )


def compare_checkpoints(before_label: str, after_label: str, expectation: str = "") -> str:
    before = get_checkpoint(before_label)
    after = get_checkpoint(after_label)
    if not before or not after:
        return "[error] Missing before/after checkpoint"
    bscreen = before.get("screen", {}) or {}
    ascreen = after.get("screen", {}) or {}
    changed = []
    if bscreen.get("app") != ascreen.get("app"):
        changed.append(f"app: {bscreen.get('app', '')} -> {ascreen.get('app', '')}")
    if bscreen.get("title") != ascreen.get("title"):
        changed.append(f"title: {bscreen.get('title', '')[:80]} -> {ascreen.get('title', '')[:80]}")
    if bscreen.get("focused") != ascreen.get("focused"):
        changed.append("focused context changed")
    if bscreen.get("ocr") != ascreen.get("ocr"):
        changed.append("visible content changed")
    status = "changed" if changed else "unchanged"

    if expectation:
        # Only match against the AFTER screen state, not the full JSON (avoids
        # false positives where the keyword appears in unrelated workspace text)
        after_screen = ascreen.get("app", "") + " " + ascreen.get("title", "") + " " + ascreen.get("focused", "")
        hay = after_screen.lower()
        tokens = [tok for tok in " ".join(expectation.lower().split()).split() if len(tok) > 3][:5]
        if tokens and changed:
            status = "matched" if any(tok in hay for tok in tokens) else "not_matched"
        elif tokens:
            status = "not_matched"

    lines = [
        "## Verification Comparison",
        f"Expectation: {expectation or 'none'}",
        f"Status: {status}",
    ]
    if changed:
        lines.append("Changes:")
        lines.extend(f"- {item}" for item in changed[:6])
    lines.append(f"After workspace: {str(after.get('workspace', '') or '')[:700]}")
    return "\n".join(lines)


def rollback_hint(task: str, result_text: str, before_label: str, after_label: str) -> str:
    before = get_checkpoint(before_label)
    after = get_checkpoint(after_label)
    if not before or not after:
        return ""
    bscreen = before.get("screen", {}) or {}
    ascreen = after.get("screen", {}) or {}
    if bscreen.get("title") and ascreen.get("title") and bscreen.get("title") != ascreen.get("title"):
        return f"Rollback hint: refocus '{bscreen.get('title', '')[:80]}' or undo the last UI action if this result is wrong."
    if bscreen.get("app") and ascreen.get("app") and bscreen.get("app") != ascreen.get("app"):
        return f"Rollback hint: return to {bscreen.get('app', '')} and verify whether the task actually completed."
    if "[error" in (result_text or "").lower():
        return "Rollback hint: revert the last step, verify current screen state, then retry with a narrower action."
    return "Rollback hint: compare the current screen with the pre-action state before continuing."
