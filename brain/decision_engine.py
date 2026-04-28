"""Option generation and recommendation for hard decisions."""

from __future__ import annotations

import json
import logging
from typing import Any

from brain.llm import get_client

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


def _normalize_options(options: Any) -> list[dict[str, Any]]:
    if not isinstance(options, list):
        return []
    out: list[dict[str, Any]] = []
    for item in options[:4]:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "name": str(item.get("name", "") or "").strip()[:80],
                "summary": str(item.get("summary", "") or "").strip()[:220],
                "pros": [str(x).strip()[:100] for x in (item.get("pros") or [])[:3]],
                "cons": [str(x).strip()[:100] for x in (item.get("cons") or [])[:3]],
                "risk": str(item.get("risk", "") or "").strip()[:120],
            }
        )
    return [o for o in out if o["name"]]


async def analyze_decision(
    user_text: str,
    assembled_context: str,
    domain: str = "general",
    task_type: str = "analyze",
) -> dict[str, Any]:
    """Generate options, tradeoffs, and a recommendation."""
    llm = get_client()
    if llm.provider == "none":
        return {
            "options": [],
            "recommendation": "",
            "why": "",
            "confidence": 0.0,
            "decision_needed": False,
        }

    prompt = f"""Analyze this problem as a decision.

Return strict JSON only:
{{
  "decision_needed": true|false,
  "options": [
    {{
      "name": "",
      "summary": "",
      "pros": [],
      "cons": [],
      "risk": ""
    }}
  ],
  "recommendation": "",
  "why": "",
  "confidence": 0.0
}}

Rules:
- If the user is mostly asking for explanation only, you may set decision_needed=false.
- If there is a meaningful choice, produce 2-4 options.
- Recommendation should take a position, not hedge pointlessly.
- Optimize for speed, reversibility, risk, and leverage.

Domain: {domain}
Task type: {task_type}

Problem:
{user_text}

Context:
{assembled_context[:4000]}
"""
    try:
        resp = await llm.create(
            messages=[{"role": "user", "content": prompt}],
            model_type="reasoning",
            max_tokens=500,
        )
        data = _parse_json_object(resp.text)
        return {
            "decision_needed": bool(data.get("decision_needed", False)),
            "options": _normalize_options(data.get("options")),
            "recommendation": str(data.get("recommendation", "") or "").strip()[:180],
            "why": str(data.get("why", "") or "").strip()[:320],
            "confidence": float(data.get("confidence", 0.0) or 0.0),
        }
    except Exception as exc:
        log.debug(f"Decision engine failed: {exc}")
        return {
            "decision_needed": False,
            "options": [],
            "recommendation": "",
            "why": "",
            "confidence": 0.0,
        }
