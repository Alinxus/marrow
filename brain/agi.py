"""
Marrow Personal AGI — Self-Improving Knowledge Engine

This is the brain behind the knowledge base. It runs continuously in the
background and uses the full RetainDB API surface to build a growing,
self-correcting model of the user's life.

What it does (in priority order):
  1. Session ingestion  — every 5 min: collects transcripts + screen obs,
     formats them as a proper conversation session, ingests into RetainDB
     so it can extract structured memories (facts, preferences, events,
     instructions) automatically.

  2. Memory extraction  — pulls raw screen content + audio through the
     /memory/extract endpoint so RetainDB types and indexes it properly
     (not just a blob of text in a blob column).

  3. Profile sync       — every 10 min: pulls RetainDB's synthesized user
     profile model (preferences, goals, working style, trust) and merges
     it into the local wiki for fast access.

  4. Gap detection      — every 15 min: asks RetainDB what it doesn't know
     about the user. Stores open questions. When screen/audio content
     answers a gap question, learns it immediately.

  5. Project indexing   — mirrors detected projects from wiki into RetainDB
     projects, ingests relevant observations into the right project.

  6. Oracle context     — on demand from reasoning loop: uses oracle search
     (semantic + lexical + phonetic + temporal + graph) to assemble
     richer context than simple vector search.

  7. Graph awareness    — periodically fetches memory graph to find
     connections the wiki hasn't captured, surfaces them as context.

  8. Learn verified facts — when reasoning produces high-confidence
     conclusions, teaches them directly via /v1/learn.

All operations are async, fire-and-forget where possible, and
gracefully degrade to local-only when RetainDB is unreachable.
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

# ─── Gap tracking persisted locally ───────────────────────────────────────────
GAP_PATH = Path.home() / ".marrow" / "gaps.json"


def _load_gaps() -> list[dict]:
    try:
        if GAP_PATH.exists():
            return json.loads(GAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_gaps(gaps: list[dict]) -> None:
    try:
        GAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        GAP_PATH.write_text(json.dumps(gaps, indent=2), encoding="utf-8")
    except Exception:
        pass


class MarrowAGI:
    """
    Self-improving personal intelligence engine.

    Runs as a background asyncio task. Never blocks the main loop.
    All public methods are safe to call from the reasoning loop.
    """

    def __init__(self):
        self._last_session_ingest = 0.0
        self._last_profile_sync = 0.0
        self._last_gap_detect = 0.0
        self._last_graph_fetch = 0.0
        self._last_project_sync = 0.0
        self._last_ingest_id = 0       # last transcript id ingested
        self._last_extract_id = 0      # last observation id extracted
        self._open_gaps: list[dict] = _load_gaps()
        self._project_id_map: dict[str, str] = {}   # project_name → retaindb id
        self._graph_summary: str = ""
        self._session_counter = 0
        self._running = False

    # ─── Main loop ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Background AGI loop. Runs forever alongside the reasoning loop."""
        self._running = True
        log.info("MarrowAGI: background loop started")

        # Stagger startup so we don't hit API at cold start
        await asyncio.sleep(90)

        while self._running:
            try:
                now = time.time()

                # Session ingest every 5 min
                if now - self._last_session_ingest > 300:
                    await self._ingest_session()

                # Memory extraction every 3 min
                if now - self._last_session_ingest > 180:
                    await self._extract_new_observations()

                # Profile model sync every 10 min
                if now - self._last_profile_sync > 600:
                    await self._sync_profile_model()

                # Gap detection every 15 min
                if now - self._last_gap_detect > 900:
                    await self._detect_gaps()

                # Project indexing every 20 min
                if now - self._last_project_sync > 1200:
                    await self._sync_projects()

                # Memory graph every 30 min
                if now - self._last_graph_fetch > 1800:
                    await self._fetch_graph()

            except Exception as e:
                log.warning(f"AGI loop error: {e}")

            await asyncio.sleep(60)

    def stop(self) -> None:
        self._running = False

    # ─── Session Ingestion ───────────────────────────────────────────────────

    async def _ingest_session(self) -> None:
        """
        Collect recent transcripts + high-value observations, format as a
        conversation session, and ingest into RetainDB.

        RetainDB's session ingestor extracts: facts, preferences, instructions,
        events, decisions — structured memories you can later query.
        """
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        try:
            # Pull transcripts since last ingest
            conn = db._connect()
            transcripts = conn.execute(
                "SELECT id, ts, text FROM transcripts WHERE id > ? ORDER BY ts ASC LIMIT 100",
                (self._last_ingest_id,),
            ).fetchall()

            # Pull high-value observations since last ingest
            observations = conn.execute(
                "SELECT id, type, content, source, ts FROM observations WHERE id > ? ORDER BY ts ASC LIMIT 50",
                (self._last_extract_id,),
            ).fetchall()

            if not transcripts and not observations:
                self._last_session_ingest = time.time()
                return

            events = []

            # Transcripts become user turns
            for row in transcripts:
                events.append({
                    "role": "user",
                    "content": row["text"],
                    "ts": row["ts"],
                })
                if row["id"] > self._last_ingest_id:
                    self._last_ingest_id = row["id"]

            # Screen observations become system context
            for row in observations:
                if row["type"] in ("screen", "app", "screen_summary"):
                    events.append({
                        "role": "system",
                        "content": f"[{row['source']}] {row['content']}",
                        "ts": row["ts"],
                    })

            if not events:
                self._last_session_ingest = time.time()
                return

            # Sort by time
            events.sort(key=lambda e: e.get("ts", 0))

            self._session_counter += 1
            session_id = f"marrow_session_{int(time.time())}_{self._session_counter}"

            # Fire-and-forget to RetainDB
            result = await client.ingest_session(
                session_id=session_id,
                events=[{"role": e["role"], "content": e["content"]} for e in events],
                metadata={
                    "source": "marrow_ambient",
                    "event_count": len(events),
                    "ts": time.time(),
                },
            )

            job_id = result.get("job_id") or result.get("id")
            log.info(f"AGI: session ingested ({len(events)} events, job={job_id})")
            self._last_session_ingest = time.time()

        except Exception as e:
            log.debug(f"AGI session ingest error: {e}")

    # ─── Memory Extraction ───────────────────────────────────────────────────

    async def _extract_new_observations(self) -> None:
        """
        Send new screen/audio observations through /memory/extract so
        RetainDB classifies and indexes them with proper memory types.
        Much richer than a raw add_memory call.
        """
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        try:
            conn = db._connect()
            # High-signal observation types worth extracting
            obs = conn.execute(
                """SELECT id, type, content, source FROM observations
                   WHERE id > ? AND type NOT IN ('screen', 'app')
                   ORDER BY ts ASC LIMIT 30""",
                (self._last_extract_id,),
            ).fetchall()

            if not obs:
                return

            # Batch into one extract call to save API credits
            combined = "\n".join(f"[{o['type']}|{o['source']}] {o['content']}" for o in obs)
            result = await client.extract_memory(combined, context="ambient_observations")

            extracted = result.get("memories", [])
            if extracted:
                log.info(f"AGI: extracted {len(extracted)} typed memories from {len(obs)} observations")

            # Advance pointer
            max_id = max(o["id"] for o in obs)
            if max_id > self._last_extract_id:
                self._last_extract_id = max_id

        except Exception as e:
            log.debug(f"AGI extraction error: {e}")

    # ─── Profile Model Sync ──────────────────────────────────────────────────

    async def _sync_profile_model(self) -> None:
        """
        Pull RetainDB's synthesized user profile and merge into local wiki.
        This gives us RetainDB's structured view: preferences, goals,
        working style, frequent entities, trust score.
        """
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        try:
            model = await client.get_profile_model()
            if model.get("error") or not model:
                return

            # Merge into wiki
            from brain.wiki import get_wiki
            wiki = get_wiki()

            # Preferences
            prefs = model.get("preferences", {})
            if isinstance(prefs, dict):
                for k, v in prefs.items():
                    wiki._wiki.setdefault("preferences", {})[k] = v

            # Goals
            goals = model.get("goals", [])
            if isinstance(goals, list):
                existing_goals = {g.get("goal", "") for g in wiki._wiki.get("goals", []) if isinstance(g, dict)}
                for g in goals:
                    goal_text = g if isinstance(g, str) else g.get("goal", str(g))
                    if goal_text and goal_text not in existing_goals:
                        wiki._wiki.setdefault("goals", []).append({
                            "goal": goal_text,
                            "priority": "medium",
                            "status": "active",
                            "source": "retaindb",
                        })

            # Frequent entities → people section
            entities = model.get("frequent_entities", [])
            if isinstance(entities, list):
                for e in entities[:10]:
                    name = e if isinstance(e, str) else e.get("name", "")
                    if name and name not in wiki._wiki.get("people", {}):
                        wiki._wiki.setdefault("people", {})[name] = {
                            "role": e.get("type", "unknown") if isinstance(e, dict) else "unknown",
                            "relationship": "frequent_contact",
                            "context": "detected by RetainDB",
                            "source": "retaindb_profile",
                        }

            # Working style → patterns
            style = model.get("working_style", "")
            if style and style not in wiki._wiki.get("patterns", []):
                wiki._wiki.setdefault("patterns", []).append(f"[RetainDB] {style}")

            wiki._wiki["last_updated"] = time.time()
            wiki.save()

            self._last_profile_sync = time.time()
            log.info("AGI: profile model synced from RetainDB")

        except Exception as e:
            log.debug(f"AGI profile sync error: {e}")

    # ─── Gap Detection ───────────────────────────────────────────────────────

    async def _detect_gaps(self) -> None:
        """
        Ask RetainDB what it doesn't know about the user.
        Stores open questions. When screen/audio later answers a gap,
        it's learned immediately via /v1/learn.
        """
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        try:
            result = await client.detect_gaps(
                focus_areas=["work", "projects", "preferences", "goals", "schedule"]
            )

            gaps = result.get("gaps", [])
            if not gaps:
                self._last_gap_detect = time.time()
                return

            # Merge with existing open gaps (don't duplicate)
            existing_questions = {g["question"] for g in self._open_gaps}
            new_gaps = []
            for gap in gaps[:10]:
                q = gap if isinstance(gap, str) else gap.get("question", str(gap))
                priority = gap.get("priority", 5) if isinstance(gap, dict) else 5
                if q and q not in existing_questions:
                    new_gaps.append({
                        "question": q,
                        "priority": priority,
                        "detected_at": time.time(),
                        "answered": False,
                    })

            self._open_gaps = sorted(
                self._open_gaps + new_gaps,
                key=lambda g: g.get("priority", 5),
                reverse=True,
            )[:30]  # keep top 30

            _save_gaps(self._open_gaps)
            self._last_gap_detect = time.time()

            if new_gaps:
                log.info(f"AGI: detected {len(new_gaps)} new knowledge gaps")

        except Exception as e:
            log.debug(f"AGI gap detection error: {e}")

    async def check_gaps_against_context(self, context_text: str) -> None:
        """
        Called by reasoning loop each cycle. Checks if current screen/audio
        content answers any open gap questions. If yes, learns the answer.
        Non-blocking — fires answer learning in background.
        """
        if not self._open_gaps:
            return

        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        # Only check top-priority unanswered gaps
        open_gaps = [g for g in self._open_gaps if not g.get("answered")][:5]
        if not open_gaps:
            return

        from brain.llm import get_client as get_llm
        llm = get_llm()

        questions = "\n".join(f"- {g['question']}" for g in open_gaps)
        prompt = f"""You are checking if any of these open questions about the user are answered in the current context.

Open questions:
{questions}

Current context:
{context_text[:1500]}

For each question that IS answered, return JSON:
{{"answered": [{{"question": "...", "answer": "..."}}]}}

If nothing is answered, return {{"answered": []}}
Return ONLY JSON."""

        try:
            resp = await llm.create(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                model_type="scoring",
            )
            raw = resp.text.strip()
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start == -1:
                return
            data = json.loads(raw[start:end])
            answered = data.get("answered", [])

            for ans in answered:
                q = ans.get("question", "")
                a = ans.get("answer", "")
                if q and a:
                    # Learn via RetainDB
                    asyncio.create_task(client.learn([{
                        "content": f"Q: {q}\nA: {a}",
                        "topic": "user_profile",
                        "confidence": 0.8,
                    }]))
                    # Mark as answered in local list
                    for gap in self._open_gaps:
                        if gap["question"] == q:
                            gap["answered"] = True
                            gap["answer"] = a
                            gap["answered_at"] = time.time()
                    log.info(f"AGI: gap answered — {q[:60]}")

            _save_gaps(self._open_gaps)

        except Exception as e:
            log.debug(f"AGI gap check error: {e}")

    # ─── Project Indexing ────────────────────────────────────────────────────

    async def _sync_projects(self) -> None:
        """
        Mirror wiki projects into RetainDB project objects.
        Ingests new observations relevant to each project.
        """
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        try:
            from brain.wiki import get_wiki
            wiki = get_wiki()
            wiki_projects = wiki._wiki.get("projects", {})
            if not wiki_projects:
                return

            # Get existing RetainDB projects
            existing = await client.get_projects()
            existing_names = {p.get("name", ""): p.get("id") for p in existing}

            for project_name, project_data in wiki_projects.items():
                if not isinstance(project_data, dict):
                    continue
                if project_data.get("status") != "active":
                    continue

                # Create project in RetainDB if it doesn't exist
                if project_name not in existing_names:
                    desc = project_data.get("description", "")
                    tech = project_data.get("tech_stack", "")
                    result = await client.create_project(
                        name=project_name,
                        description=f"{desc} [{tech}]" if tech else desc,
                    )
                    project_id = result.get("id")
                    if project_id:
                        existing_names[project_name] = project_id
                        log.info(f"AGI: created RetainDB project '{project_name}'")
                else:
                    project_id = existing_names[project_name]

                self._project_id_map[project_name] = project_id or ""

            self._last_project_sync = time.time()

        except Exception as e:
            log.debug(f"AGI project sync error: {e}")

    async def ingest_into_project(self, content: str, project_name: str, source: str = "screen") -> None:
        """Ingest content into a specific RetainDB project."""
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        project_id = self._project_id_map.get(project_name)
        if not project_id:
            return

        try:
            await client.ingest_project(project_id, content, source)
        except Exception as e:
            log.debug(f"AGI project ingest error: {e}")

    # ─── Memory Graph ────────────────────────────────────────────────────────

    async def _fetch_graph(self) -> None:
        """
        Fetch memory graph and extract useful connection summaries.
        These supplement the wiki with cross-memory relationships that
        the LLM wiki updater might not capture.
        """
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return

        try:
            graph = await client.get_graph()
            nodes = graph.get("nodes", [])
            edges = graph.get("edges", [])

            if not nodes:
                self._last_graph_fetch = time.time()
                return

            # Summarize top connections
            lines = []
            # High-weight edges = strong associations
            strong = sorted(edges, key=lambda e: e.get("weight", 0), reverse=True)[:10]
            for edge in strong:
                src = edge.get("source_label") or edge.get("source", "")
                dst = edge.get("target_label") or edge.get("target", "")
                rel = edge.get("relationship", "relates_to")
                weight = edge.get("weight", 0)
                if src and dst:
                    lines.append(f"  {src} —[{rel}]→ {dst} (strength: {weight:.2f})")

            self._graph_summary = "\n".join(lines) if lines else ""
            self._last_graph_fetch = time.time()

            if lines:
                log.info(f"AGI: graph fetched ({len(nodes)} nodes, {len(edges)} edges)")

        except Exception as e:
            log.debug(f"AGI graph fetch error: {e}")

    def get_graph_context(self) -> str:
        """Return graph summary for inclusion in reasoning prompt."""
        if not self._graph_summary:
            return ""
        return f"=== MEMORY CONNECTIONS ===\n{self._graph_summary}"

    # ─── Oracle Context ──────────────────────────────────────────────────────

    async def get_oracle_context(self, query: str, limit: int = 8) -> str:
        """
        Best-quality context retrieval for the reasoning loop.
        Uses oracle search: semantic + lexical + phonetic + temporal + graph.
        Falls back to semantic search, then local.
        """
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return ""

        try:
            resp = await client.oracle_search(query, limit=limit, include_graph=True)
            results = resp.get("results", [])

            if not results:
                # Fallback to context query endpoint
                ctx = await client.query_context(query, include_profile=True)
                content = ctx.get("context") or ctx.get("content") or ""
                return content[:2000]

            lines = [f"=== ORACLE MEMORY ({len(results)} results) ==="]
            for r in results:
                mtype = r.get("memory_type", "mem")
                content = r.get("content", "")[:200]
                score = r.get("score", 0)
                lines.append(f"[{mtype}|{score:.2f}] {content}")

            return "\n".join(lines)[:3000]

        except Exception as e:
            log.debug(f"AGI oracle error: {e}")
            return ""

    # ─── Learn Verified Facts ────────────────────────────────────────────────

    async def learn_fact(self, fact: str, topic: str = "user_profile", confidence: float = 0.85) -> None:
        """Teach a high-confidence fact directly to RetainDB's learn endpoint."""
        from actions.memory import get_memory_client
        client = get_memory_client()
        if not client:
            return
        try:
            await client.learn([{
                "content": fact,
                "topic": topic,
                "confidence": confidence,
            }])
            log.debug(f"AGI learned: {fact[:60]}")
        except Exception as e:
            log.debug(f"AGI learn error: {e}")

    # ─── Gap Status ──────────────────────────────────────────────────────────

    def get_open_gaps(self) -> list[dict]:
        """Return unanswered gap questions (for dashboard/debug)."""
        return [g for g in self._open_gaps if not g.get("answered")]

    def get_answered_gaps(self) -> list[dict]:
        """Return answered gap questions."""
        return [g for g in self._open_gaps if g.get("answered")]

    def get_stats(self) -> dict:
        """AGI statistics for dashboard/monitoring."""
        open_gaps = self.get_open_gaps()
        answered = self.get_answered_gaps()
        return {
            "open_gaps": len(open_gaps),
            "answered_gaps": len(answered),
            "projects_tracked": len(self._project_id_map),
            "sessions_ingested": self._session_counter,
            "last_session_ingest": datetime.fromtimestamp(self._last_session_ingest).isoformat()
            if self._last_session_ingest else "never",
            "last_profile_sync": datetime.fromtimestamp(self._last_profile_sync).isoformat()
            if self._last_profile_sync else "never",
            "top_gaps": [g["question"][:80] for g in open_gaps[:3]],
            "graph_connections": self._graph_summary.count("→"),
        }


# ─── Global singleton ─────────────────────────────────────────────────────────

_agi: Optional[MarrowAGI] = None


def get_agi() -> MarrowAGI:
    global _agi
    if _agi is None:
        _agi = MarrowAGI()
    return _agi


async def agi_loop() -> None:
    """Top-level coroutine for main.py task supervisor."""
    await get_agi().run()
