"""
Personal Wiki — Marrow's living knowledge base about the user.

A continuously-updated structured document that captures everything Marrow
learns: identity, projects, people, goals, preferences, behavioral patterns.

This is what makes context semantically assembled rather than just recency-
based. Every reasoning call gets the full wiki + RetainDB semantic search
results, not just a rolling time window.

Update cycle:
  - On startup: loads from ~/.marrow/wiki.json
  - Every 5 min (background): LLM merges new observations into wiki
  - On explicit "remember X": immediate update
  - On RetainDB sync: pushes wiki summary as a single high-priority memory

Structure:
  identity   — who the user is (name, role, location, work hours)
  projects   — active/past projects with status, tools, collaborators
  people     — people they interact with (role, relationship, notes)
  goals      — current goals with status and priority
  preferences — how they like to work, communicate, use tools
  patterns   — behavioral patterns observed over time
  facts      — other durable facts
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from storage import db

log = logging.getLogger(__name__)

WIKI_PATH = Path.home() / ".marrow" / "wiki.json"

_EMPTY_WIKI = {
    "identity": {},
    "projects": {},
    "people": {},
    "goals": [],
    "preferences": {},
    "patterns": [],
    "facts": [],
    "last_updated": 0.0,
    "last_obs_id": 0,
}

_WIKI_UPDATE_PROMPT = """\
You maintain a personal knowledge base about a user based on observations from their screen and audio.

Current wiki (JSON):
{wiki}

New observations since last update:
{observations}

Merge the new observations into the wiki. Update, add, or refine entries.
Rules:
- identity: name, job title, company, location, timezone, typical work hours
- projects: dict of project_name → {{description, status (active/paused/done), tech_stack, collaborators, notes}}
- people: dict of person_name → {{role, relationship (colleague/manager/client/friend), context, last_mentioned}}
- goals: list of {{goal, priority (high/medium/low), status (active/done/blocked), notes}}
- preferences: flat dict of preference facts (e.g. "prefers_dark_mode": true, "primary_language": "Python")
- patterns: list of behavioral observations (e.g. "works late on Thursdays", "uses VSCode for Python")
- facts: list of other durable facts that don't fit above

Rules:
- Only add things that are DURABLE and SPECIFIC. Skip transient details.
- If something is already in the wiki, update it rather than duplicating.
- Remove facts that are clearly outdated.
- Do not hallucinate — only extract what is genuinely in the observations.
- Keep each entry concise (one sentence max for facts/patterns).

Return ONLY valid JSON matching the wiki structure. No markdown, no commentary."""

_WIKI_RETAINDB_SUMMARY_PROMPT = """\
Summarize this personal knowledge base as a dense paragraph for a memory system.
Include: who the person is, what they're working on, their key goals, important people, and notable patterns.
Max 300 words. Write in third person about "the user".

Wiki:
{wiki}"""


class WikiManager:
    def __init__(self):
        self._wiki: dict = {}
        self._lock = asyncio.Lock()
        self._last_update = 0.0
        self._update_interval = 300  # 5 min

    def load(self) -> None:
        """Load wiki from disk on startup."""
        try:
            if WIKI_PATH.exists():
                self._wiki = json.loads(WIKI_PATH.read_text(encoding="utf-8"))
                log.info(f"Wiki loaded: {len(self._wiki.get('people', {}))} people, "
                         f"{len(self._wiki.get('projects', {}))} projects")
            else:
                self._wiki = dict(_EMPTY_WIKI)
                log.info("Wiki: starting fresh")
        except Exception as e:
            log.warning(f"Wiki load error: {e} — starting fresh")
            self._wiki = dict(_EMPTY_WIKI)

    def save(self) -> None:
        """Persist wiki to disk."""
        try:
            WIKI_PATH.parent.mkdir(parents=True, exist_ok=True)
            WIKI_PATH.write_text(
                json.dumps(self._wiki, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning(f"Wiki save error: {e}")

    def to_prompt_context(self) -> str:
        """Format wiki as dense context for the reasoning prompt."""
        w = self._wiki
        if not w or w == _EMPTY_WIKI:
            return ""

        lines = ["=== PERSONAL KNOWLEDGE BASE ==="]

        # Identity
        ident = w.get("identity", {})
        if ident:
            parts = []
            if ident.get("name"):
                parts.append(ident["name"])
            if ident.get("role"):
                parts.append(ident["role"])
            if ident.get("company"):
                parts.append(f"at {ident['company']}")
            if ident.get("location"):
                parts.append(f"in {ident['location']}")
            if parts:
                lines.append(f"**Who:** {', '.join(parts)}")
            if ident.get("work_hours"):
                lines.append(f"**Works:** {ident['work_hours']}")

        # Projects
        projects = w.get("projects", {})
        active = {k: v for k, v in projects.items()
                  if isinstance(v, dict) and v.get("status") == "active"}
        if active:
            lines.append("**Active projects:**")
            for name, p in list(active.items())[:5]:
                desc = p.get("description", "")
                tech = p.get("tech_stack", "")
                notes = p.get("notes", "")
                entry = f"  • {name}"
                if desc:
                    entry += f" — {desc}"
                if tech:
                    entry += f" [{tech}]"
                if notes:
                    entry += f" ({notes})"
                lines.append(entry)

        # Goals
        goals = w.get("goals", [])
        active_goals = [g for g in goals
                        if isinstance(g, dict) and g.get("status") == "active"]
        if active_goals:
            lines.append("**Goals:**")
            for g in active_goals[:5]:
                priority = g.get("priority", "")
                text = g.get("goal", str(g))
                p_tag = f"[{priority}] " if priority else ""
                lines.append(f"  • {p_tag}{text}")

        # People
        people = w.get("people", {})
        if people:
            lines.append("**People:**")
            for name, p in list(people.items())[:8]:
                if not isinstance(p, dict):
                    continue
                rel = p.get("relationship", "")
                role = p.get("role", "")
                ctx = p.get("context", "")
                entry = f"  • {name}"
                if role:
                    entry += f" ({role}"
                    if rel:
                        entry += f", {rel}"
                    entry += ")"
                if ctx:
                    entry += f" — {ctx}"
                lines.append(entry)

        # Preferences (condensed)
        prefs = w.get("preferences", {})
        if prefs:
            pref_parts = [f"{k}: {v}" for k, v in list(prefs.items())[:6]]
            if pref_parts:
                lines.append(f"**Prefs:** {' | '.join(pref_parts)}")

        # Patterns
        patterns = w.get("patterns", [])
        if patterns:
            lines.append("**Patterns:**")
            for p in patterns[:5]:
                lines.append(f"  • {p}")

        # Facts
        facts = w.get("facts", [])
        if facts:
            lines.append("**Facts:**")
            for f in facts[:8]:
                lines.append(f"  • {f}")

        updated = self._wiki.get("last_updated", 0)
        if updated:
            ago = int((time.time() - updated) / 60)
            lines.append(f"_(wiki updated {ago}m ago)_")

        return "\n".join(lines)

    async def update_from_observations(self, force: bool = False) -> None:
        """
        Pull new observations from DB, ask LLM to merge into wiki.
        Runs in background — never blocks reasoning loop.
        """
        now = time.time()
        if not force and (now - self._last_update) < self._update_interval:
            return

        async with self._lock:
            try:
                last_id = self._wiki.get("last_obs_id", 0)

                # Get new observations since last wiki update
                new_obs = db.get_observations_since_id(last_id, limit=50)
                if not new_obs and not force:
                    return

                # Also get recent transcripts (what the user said)
                recent_tx = db.get_recent_transcripts(window_seconds=3600)

                obs_lines = []
                max_id = last_id
                for o in new_obs:
                    obs_lines.append(f"[{o['type']}] {o['content']}")
                    if o.get("id", 0) > max_id:
                        max_id = o["id"]

                for t in recent_tx[-10:]:
                    obs_lines.append(f"[speech] {t['text']}")

                if not obs_lines:
                    return

                from brain.llm import get_client
                llm = get_client()

                wiki_json = json.dumps(self._wiki, indent=2)
                obs_text = "\n".join(obs_lines[:80])

                prompt = _WIKI_UPDATE_PROMPT.format(
                    wiki=wiki_json[:3000],
                    observations=obs_text[:2000],
                )

                response = await llm.create(
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1500,
                    model_type="scoring",
                )
                raw = response.text.strip()

                # Parse JSON — strip markdown fences if present
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"):
                        raw = raw[4:]
                raw = raw.strip()

                start = raw.find("{")
                end = raw.rfind("}") + 1
                if start == -1 or end == 0:
                    log.debug("Wiki update: no JSON returned")
                    return

                updated = json.loads(raw[start:end])
                updated["last_updated"] = now
                updated["last_obs_id"] = max_id

                self._wiki = updated
                self.save()

                # Sync summary to RetainDB in background
                asyncio.create_task(self._sync_to_retaindb())

                log.info(f"Wiki updated: {len(new_obs)} new observations merged")
                self._last_update = now

            except json.JSONDecodeError as e:
                log.debug(f"Wiki update JSON error: {e}")
            except Exception as e:
                log.warning(f"Wiki update error: {e}")

    async def _sync_to_retaindb(self) -> None:
        """
        Push wiki to RetainDB two ways:
        1. Summary memory — searchable by oracle
        2. Bulk fact learn — verified facts via /v1/learn
        """
        try:
            from actions.memory import get_memory_client
            client = get_memory_client()
            if not client:
                return

            from brain.llm import get_client
            llm = get_client()

            wiki_json = json.dumps(self._wiki, indent=2)
            prompt = _WIKI_RETAINDB_SUMMARY_PROMPT.format(wiki=wiki_json[:3000])

            resp = await llm.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=400,
                model_type="scoring",
            )
            summary = resp.text.strip()

            # 1. Summary memory
            await client.add_memory(
                f"[WIKI SUMMARY] {summary}",
                memory_type="factual",
                session_id="wiki_sync",
            )

            # 2. Teach verified facts via /v1/learn
            facts = []
            ident = self._wiki.get("identity", {})
            if ident.get("name"):
                facts.append({"content": f"User's name is {ident['name']}", "topic": "identity", "confidence": 0.95})
            if ident.get("role"):
                facts.append({"content": f"User's role is {ident['role']}", "topic": "identity", "confidence": 0.9})
            for pref_k, pref_v in list(self._wiki.get("preferences", {}).items())[:5]:
                facts.append({"content": f"User preference: {pref_k} = {pref_v}", "topic": "preferences", "confidence": 0.85})
            for pattern in self._wiki.get("patterns", [])[:5]:
                facts.append({"content": f"User behavioral pattern: {pattern}", "topic": "patterns", "confidence": 0.8})

            if facts:
                await client.learn(facts)

            log.debug(f"Wiki synced to RetainDB ({len(facts)} facts learned)")
        except Exception as e:
            log.debug(f"Wiki RetainDB sync error: {e}")

    def patch(self, section: str, key: str, value) -> None:
        """Directly patch a wiki section (used by explicit user instructions)."""
        if section not in self._wiki:
            self._wiki[section] = {} if section not in ("goals", "patterns", "facts") else []
        if isinstance(self._wiki[section], dict):
            self._wiki[section][key] = value
        elif isinstance(self._wiki[section], list) and value not in self._wiki[section]:
            self._wiki[section].append(value)
        self._wiki["last_updated"] = time.time()
        self.save()


# ─── Global instance ─────────────────────────────────────────────────────────

_wiki: Optional[WikiManager] = None


def get_wiki() -> WikiManager:
    global _wiki
    if _wiki is None:
        _wiki = WikiManager()
        _wiki.load()
    return _wiki


async def wiki_update_loop() -> None:
    """Background loop that updates wiki every 5 minutes."""
    wiki = get_wiki()
    # Initial update 60s after startup
    await asyncio.sleep(60)
    while True:
        try:
            await wiki.update_from_observations()
        except Exception as e:
            log.warning(f"Wiki loop error: {e}")
        await asyncio.sleep(120)  # check every 2 min, updates every 5


def wiki_context() -> str:
    """Get wiki as prompt context string."""
    return get_wiki().to_prompt_context()
