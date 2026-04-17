"""
Memory module — local SQLite + RetainDB cloud.

Architecture:
  Local (instant, ~1ms):
    - SQLite FTS5 for search
    - LRU cache for hot data
    - Thread-safe WAL mode

  RetainDB (full API surface):
    - Session ingestion (entire conversation → structured memories)
    - Memory extraction (raw text → typed memories automatically)
    - User profile model (RetainDB's synthesized view of the user)
    - Gap detection (what does it NOT know about the user?)
    - Oracle search (advanced semantic + fact retrieval)
    - Memory graph (connections between memories)
    - Context query (retrieval-augmented context assembly)
    - Learn endpoint (teach verified facts)
    - Projects + Sources (organize knowledge by domain)
    - Bulk memory writes
    - File ingestion

All RetainDB writes are fire-and-forget (never block main loop).
All RetainDB reads fall back to local if cloud fails.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

import httpx

import config
from storage import db

log = logging.getLogger(__name__)

RETAINDB_API_KEY = os.environ.get("RETAINDB_API_KEY", "")
RETAINDB_PROJECT = os.environ.get("RETAINDB_PROJECT", "marrow")
RETAINDB_BASE_URL = "https://api.retaindb.com/v1"
MEMORY_USER_ID = "marrow_user"


# ─── LRU Cache ────────────────────────────────────────────────────────────────


class MemoryCache:
    def __init__(self, max_size: int = 100, ttl: int = 30):
        self._cache: OrderedDict = OrderedDict()
        self._ttl = ttl
        self._max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        h = hashlib.md5(key.encode()).hexdigest()
        if h not in self._cache:
            return None
        item = self._cache[h]
        if time.time() - item["ts"] > self._ttl:
            del self._cache[h]
            return None
        self._cache.move_to_end(h)
        return item["value"]

    def set(self, key: str, value: Any) -> None:
        h = hashlib.md5(key.encode()).hexdigest()
        if h in self._cache:
            self._cache.move_to_end(h)
        self._cache[h] = {"value": value, "ts": time.time()}
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def invalidate(self, key: str) -> None:
        h = hashlib.md5(key.encode()).hexdigest()
        self._cache.pop(h, None)

    def invalidate_prefix(self, prefix: str) -> None:
        keys = [k for k in list(self._cache.keys())]
        for k in keys:
            item = self._cache.get(k)
            if item and prefix in str(item.get("value", "")):
                del self._cache[k]


_memory_cache = MemoryCache(max_size=200, ttl=30)
_profile_cache = MemoryCache(max_size=10, ttl=120)


# ─── RetainDB HTTP Client ─────────────────────────────────────────────────────


class RetainDBClient:
    """
    Full RetainDB API client.
    Covers: memory CRUD, session ingestion, extraction, profile model,
    gap detection, oracle search, memory graph, context query, projects,
    sources, files, learn, bulk writes, semantic search, usage stats.
    """

    def __init__(self, api_key: str, project: str):
        self.api_key = api_key
        self.project = project
        self.base_url = RETAINDB_BASE_URL
        self._http: Optional[httpx.AsyncClient] = None

    def _headers(self, content_type: str = "application/json") -> dict:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=15.0)
        return self._http

    async def _post(self, path: str, body: dict, timeout: float = 15.0) -> dict:
        try:
            http = await self._get_http()
            resp = await http.post(
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=body,
                timeout=timeout,
            )
            return resp.json() if resp.content else {}
        except Exception as e:
            log.debug(f"RetainDB POST {path}: {e}")
            return {"error": str(e)}

    async def _get(self, path: str, params: dict = None, timeout: float = 10.0) -> dict:
        try:
            http = await self._get_http()
            resp = await http.get(
                f"{self.base_url}{path}",
                headers=self._headers(),
                params=params or {},
                timeout=timeout,
            )
            return resp.json() if resp.content else {}
        except Exception as e:
            log.debug(f"RetainDB GET {path}: {e}")
            return {"error": str(e)}

    async def _put(self, path: str, body: dict) -> dict:
        try:
            http = await self._get_http()
            resp = await http.put(
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=body,
                timeout=10.0,
            )
            return resp.json() if resp.content else {}
        except Exception as e:
            log.debug(f"RetainDB PUT {path}: {e}")
            return {"error": str(e)}

    async def _delete(self, path: str) -> dict:
        try:
            http = await self._get_http()
            resp = await http.delete(
                f"{self.base_url}{path}",
                headers=self._headers(),
                timeout=10.0,
            )
            return resp.json() if resp.content else {}
        except Exception as e:
            log.debug(f"RetainDB DELETE {path}: {e}")
            return {"error": str(e)}

    # ── Memory CRUD ──────────────────────────────────────────────────────────

    async def add_memory(
        self,
        content: str,
        memory_type: str = "factual",
        session_id: str = None,
        metadata: dict = None,
    ) -> dict:
        """POST /v1/memory — store a single memory."""
        return await self._post(
            "/memory",
            {
                "project": self.project,
                "user_id": MEMORY_USER_ID,
                "session_id": session_id or f"session_{int(time.time())}",
                "content": content,
                "memory_type": memory_type,
                "write_mode": "async",
                **({"metadata": metadata} if metadata else {}),
            },
        )

    async def bulk_memory(self, memories: list[dict]) -> dict:
        """POST /v1/memory/bulk — store many memories in one call."""
        items = []
        for m in memories:
            items.append(
                {
                    "project": self.project,
                    "user_id": MEMORY_USER_ID,
                    "session_id": m.get("session_id", f"bulk_{int(time.time())}"),
                    "content": m["content"],
                    "memory_type": m.get("memory_type", "factual"),
                    **({"metadata": m["metadata"]} if m.get("metadata") else {}),
                }
            )
        return await self._post("/memory/bulk", {"memories": items})

    async def ingest_session(
        self,
        session_id: str,
        events: list[dict],
        metadata: dict = None,
    ) -> dict:
        """
        POST /v1/memory/ingest/session
        Ingest a full conversation session — RetainDB extracts structured
        memories automatically (facts, preferences, events, instructions).

        events format: [{"role": "user"|"assistant"|"system", "content": "..."}]
        """
        return await self._post(
            "/memory/ingest/session",
            {
                "project": self.project,
                "user_id": MEMORY_USER_ID,
                "session_id": session_id,
                "events": events,
                **({"metadata": metadata} if metadata else {}),
            },
            timeout=30.0,
        )

    async def extract_memory(self, text: str, context: str = "") -> dict:
        """
        POST /v1/memory/extract
        Extract typed memories from arbitrary text (screen content, audio transcript, etc).
        Returns list of extracted memory objects with types.
        """
        body: dict = {
            "project": self.project,
            "user_id": MEMORY_USER_ID,
            "text": text,
        }
        if context:
            body["context"] = context
        return await self._post("/memory/extract", body, timeout=20.0)

    async def extract_session(self, session_id: str, events: list[dict]) -> dict:
        """POST /v1/memory/extract/session — extract from session without storing."""
        return await self._post(
            "/memory/extract/session",
            {
                "project": self.project,
                "user_id": MEMORY_USER_ID,
                "session_id": session_id,
                "events": events,
            },
            timeout=20.0,
        )

    async def search_memory(
        self,
        query: str,
        limit: int = 10,
        memory_types: list[str] = None,
    ) -> list:
        """POST /v1/memory/search — semantic search over memories."""
        body: dict = {
            "project": self.project,
            "user_id": MEMORY_USER_ID,
            "query": query,
            "limit": limit,
            "include_pending": True,
        }
        if memory_types:
            body["memory_types"] = memory_types
        resp = await self._post("/memory/search", body)
        return resp.get("results", [])

    async def get_memory(self, memory_id: str) -> dict:
        """GET /v1/memory/:memoryId — fetch a single memory."""
        return await self._get(f"/memory/{memory_id}")

    async def update_memory(
        self, memory_id: str, content: str, metadata: dict = None
    ) -> dict:
        """PUT /v1/memory/:memoryId — update a memory."""
        body: dict = {"content": content}
        if metadata:
            body["metadata"] = metadata
        return await self._put(f"/memory/{memory_id}", body)

    async def delete_memory(self, memory_id: str) -> dict:
        """DELETE /v1/memory/:memoryId."""
        return await self._delete(f"/memory/{memory_id}")

    async def get_memories_by_user(self, limit: int = 50) -> list:
        """GET /v1/memories/:id — all memories for the user."""
        resp = await self._get(f"/memories/{MEMORY_USER_ID}", {"limit": limit})
        return resp.get("memories", [])

    # ── User Profile Model ────────────────────────────────────────────────────

    async def get_profile_model(self) -> dict:
        """
        GET /v1/memory/profile/:userId/model
        RetainDB's synthesized model of the user — preferences, goals,
        working style, frequent entities, trust score. This is the crown
        jewel of the API: a structured profile built from all memories.
        """
        return await self._get(f"/memory/profile/{MEMORY_USER_ID}/model")

    async def detect_gaps(self, focus_areas: list[str] = None) -> dict:
        """
        POST /v1/memory/profile/:userId/gaps
        Find knowledge gaps — what doesn't RetainDB know about this user?
        Returns prioritized list of questions to fill gaps.
        """
        body: dict = {"project": self.project}
        if focus_areas:
            body["focus_areas"] = focus_areas
        return await self._post(f"/memory/profile/{MEMORY_USER_ID}/gaps", body)

    async def ask_profile(self, question: str) -> dict:
        """
        POST /v1/memory/profile/:userId/ask
        Ask a question about the user profile. RetainDB answers from
        accumulated memories — like querying a personal knowledge graph.
        """
        return await self._post(
            f"/memory/profile/{MEMORY_USER_ID}/ask",
            {
                "project": self.project,
                "question": question,
            },
        )

    # ── Memory Graph ──────────────────────────────────────────────────────────

    async def get_graph(self) -> dict:
        """GET /v1/memory/graph — full memory graph for the user."""
        return await self._get(
            "/memory/graph", {"user_id": MEMORY_USER_ID, "project": self.project}
        )

    async def get_conversation_graph(self, session_id: str) -> dict:
        """GET /v1/memory/graph/conversation/:sessionId."""
        return await self._get(f"/memory/graph/conversation/{session_id}")

    # ── Oracle + Semantic Search ──────────────────────────────────────────────

    async def oracle_search(
        self,
        query: str,
        limit: int = 10,
        include_graph: bool = False,
    ) -> dict:
        """
        POST /v1/oracle/search
        Advanced search: combines semantic, lexical, temporal, phonetic,
        and graph traversal. Best retrieval quality in the API.
        """
        return await self._post(
            "/oracle/search",
            {
                "project": self.project,
                "user_id": MEMORY_USER_ID,
                "query": query,
                "limit": limit,
                "include_graph": include_graph,
            },
            timeout=20.0,
        )

    async def semantic_search(self, query: str, limit: int = 10) -> list:
        """POST /v1/search/semantic — pure vector search."""
        resp = await self._post(
            "/search/semantic",
            {
                "project": self.project,
                "user_id": MEMORY_USER_ID,
                "query": query,
                "limit": limit,
            },
        )
        return resp.get("results", [])

    # ── Context ───────────────────────────────────────────────────────────────

    async def query_context(
        self,
        query: str,
        session_id: str = None,
        include_profile: bool = True,
    ) -> dict:
        """
        POST /v1/context/query
        Retrieve assembled context for a query — combines relevant memories,
        profile model, and graph neighbors into a single prompt-ready block.
        """
        body: dict = {
            "project": self.project,
            "user_id": MEMORY_USER_ID,
            "query": query,
            "include_profile": include_profile,
        }
        if session_id:
            body["session_id"] = session_id
        return await self._post("/context/query", body, timeout=20.0)

    async def share_context(self, session_id: str, expires_in: int = 3600) -> dict:
        """POST /v1/context/share — share context externally."""
        return await self._post(
            "/context/share",
            {
                "project": self.project,
                "session_id": session_id,
                "expires_in": expires_in,
            },
        )

    # ── Learn ─────────────────────────────────────────────────────────────────

    async def learn(self, facts: list[dict]) -> dict:
        """
        POST /v1/learn
        Teach the system verified facts directly.
        facts: [{"content": "...", "topic": "...", "confidence": 0-1}]
        """
        return await self._post(
            "/learn",
            {
                "project": self.project,
                "user_id": MEMORY_USER_ID,
                "facts": facts,
            },
            timeout=20.0,
        )

    # ── Projects ──────────────────────────────────────────────────────────────

    async def get_projects(self) -> list:
        """GET /v1/projects."""
        resp = await self._get("/projects")
        return resp.get("projects", [])

    async def create_project(self, name: str, description: str = "") -> dict:
        """POST /v1/projects."""
        return await self._post(
            "/projects",
            {
                "name": name,
                "description": description,
            },
        )

    async def resolve_project(self, name: str) -> dict:
        """GET /v1/projects/resolve — get project by name."""
        return await self._get("/projects/resolve", {"name": name})

    async def get_project(self, project_id: str) -> dict:
        """GET /v1/projects/:id."""
        return await self._get(f"/projects/{project_id}")

    async def ingest_project(
        self, project_id: str, content: str, source: str = "screen"
    ) -> dict:
        """POST /v1/projects/:projectId/ingest — ingest content into a project."""
        return await self._post(
            f"/projects/{project_id}/ingest",
            {
                "content": content,
                "source": source,
            },
            timeout=30.0,
        )

    async def get_project_sources(self, project_id: str) -> list:
        """GET /v1/projects/:projectId/sources."""
        resp = await self._get(f"/projects/{project_id}/sources")
        return resp.get("sources", [])

    async def add_project_source(
        self, project_id: str, source_url: str, source_type: str = "text"
    ) -> dict:
        """POST /v1/projects/:projectId/add_source."""
        return await self._post(
            f"/projects/{project_id}/add_source",
            {
                "url": source_url,
                "type": source_type,
            },
        )

    # ── Files ─────────────────────────────────────────────────────────────────

    async def upload_file(self, file_path: str, scope: str = "USER") -> dict:
        """POST /v1/files — upload a file for memory extraction."""
        try:
            http = await self._get_http()
            with open(file_path, "rb") as f:
                files = {"file": (Path(file_path).name, f, "application/octet-stream")}
                data = {"path": file_path, "scope": scope, "project_id": self.project}
                resp = await http.post(
                    f"{self.base_url}/files",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files=files,
                    data=data,
                    timeout=60.0,
                )
                return resp.json() if resp.content else {}
        except Exception as e:
            log.warning(f"RetainDB upload_file: {e}")
            return {"error": str(e)}

    async def list_files(self, prefix: str = "", scope: str = "USER") -> list:
        """GET /v1/files."""
        resp = await self._get(
            "/files", {"prefix": prefix, "scope": scope, "limit": 50}
        )
        return resp.get("files", [])

    async def ingest_file(self, file_id: str) -> dict:
        """POST /v1/files/:fileId/ingest — trigger memory extraction from a file."""
        return await self._post(f"/files/{file_id}/ingest", {}, timeout=60.0)

    async def delete_file(self, file_id: str) -> dict:
        """DELETE /v1/files/:fileId."""
        return await self._delete(f"/files/{file_id}")

    # ── Usage / Health ────────────────────────────────────────────────────────

    async def get_usage(self) -> dict:
        """GET /v1/usage — token/memory usage stats."""
        return await self._get("/usage")

    async def health(self) -> bool:
        """GET /health — check if API is reachable."""
        try:
            http = await self._get_http()
            resp = await http.get(
                f"{self.base_url.rsplit('/v1', 1)[0]}/health", timeout=5.0
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()


# ─── Singleton ────────────────────────────────────────────────────────────────

_client: Optional[RetainDBClient] = None


def get_memory_client() -> Optional[RetainDBClient]:
    global _client
    if not RETAINDB_API_KEY:
        return None
    if _client is None:
        _client = RetainDBClient(RETAINDB_API_KEY, RETAINDB_PROJECT)
    return _client


def _bg(coro) -> None:
    """Fire-and-forget: schedule coro without blocking."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(coro)
        else:
            loop.run_until_complete(coro)
    except Exception:
        pass


# ─── Local fast ops ───────────────────────────────────────────────────────────


def _insert_local_conversation(
    ts: float, role: str, content: str, context: str = ""
) -> None:
    try:
        db.insert_conversation(ts, role, content[:2000], context)
    except Exception as e:
        log.debug(f"Local conv insert: {e}")


def _insert_local_action(
    ts: float, task: str, result: str, tool: str, success: int
) -> None:
    try:
        db.insert_action(ts, task, result[:500], tool, success)
    except Exception as e:
        log.debug(f"Local action insert: {e}")


def _insert_local_observation(obs_type: str, content: str, source: str) -> bool:
    try:
        return db.insert_observation(obs_type, content, source)
    except Exception as e:
        log.debug(f"Local obs insert: {e}")
        return False


# ─── Public memory API ────────────────────────────────────────────────────────


async def memory_add(content: str, memory_type: str = "factual") -> str:
    """Add a memory — instant local write + async cloud."""
    ts = time.time()
    _insert_local_conversation(ts, "memory", content, f"type:{memory_type}")
    _profile_cache.invalidate("profile")
    client = get_memory_client()
    if client:
        _bg(client.add_memory(content, memory_type))
    return f"[memory saved] {content[:60]}"


async def memory_search(query: str, use_oracle: bool = False) -> str:
    """
    Search memories. Oracle mode = deep semantic + graph traversal (best quality).
    Falls back local if RetainDB is unavailable.
    """
    cache_key = f"search:{'oracle' if use_oracle else 'std'}:{query}"
    cached = _memory_cache.get(cache_key)
    if cached:
        return cached

    results = []
    client = get_memory_client()

    # Cloud search first (best quality)
    if client:
        try:
            if use_oracle:
                resp = await client.oracle_search(query, limit=8, include_graph=True)
                cloud_results = resp.get("results", [])
            else:
                cloud_results = await client.search_memory(query, limit=8)
            results.extend(
                [
                    f"[{r.get('memory_type', 'mem')}] {r.get('content', '')[:200]}"
                    for r in cloud_results
                ]
            )
        except Exception as e:
            log.debug(f"Cloud search error: {e}")

    # Local FTS5 fallback / supplement
    if not results:
        obs = db.search_observations(query, 8)
        results.extend([f"[observed] {o['content'][:170]}" for o in obs])
        convs = db.search_conversations(query, 5)
        results.extend([f"[conv] {c['content'][:150]}" for c in convs])
        acts = db.search_actions(query, 5)
        results.extend([f"[action] {a['task']}: {a['result'][:100]}" for a in acts])

    output = "\n".join(results) if results else "No memories found."
    _memory_cache.set(cache_key, output[:3000])
    return output[:3000]


async def memory_ask(question: str) -> str:
    """
    Ask the RetainDB user profile a question.
    e.g. "What programming languages does the user prefer?"
    """
    client = get_memory_client()
    if not client:
        return await memory_search(question)

    resp = await client.ask_profile(question)
    answer = resp.get("answer") or resp.get("content") or ""
    confidence = resp.get("confidence", 0)
    if not answer:
        return "No answer found in memory."

    return f"[{confidence:.0%} confidence] {answer}"


async def memory_get_profile() -> str:
    """Get comprehensive user profile from RetainDB + local cache."""
    cached = _profile_cache.get("profile")
    if cached:
        return cached

    client = get_memory_client()
    if client:
        try:
            model = await client.get_profile_model()
            if model and not model.get("error"):
                # Format RetainDB's synthesized profile
                lines = ["## User Profile (RetainDB)"]
                if model.get("preferences"):
                    lines.append(
                        f"Preferences: {json.dumps(model['preferences'])[:300]}"
                    )
                if model.get("goals"):
                    lines.append(f"Goals: {json.dumps(model['goals'])[:200]}")
                if model.get("working_style"):
                    lines.append(f"Style: {model['working_style'][:200]}")
                if model.get("frequent_entities"):
                    entities = model["frequent_entities"][:8]
                    lines.append(f"Frequent: {', '.join(str(e) for e in entities)}")
                if model.get("trust_score"):
                    lines.append(f"Trust: {model['trust_score']:.2f}")
                output = "\n".join(lines)
                _profile_cache.set("profile", output)
                return output
        except Exception as e:
            log.debug(f"Profile model error: {e}")

    # Local fallback
    lines = ["## Memory Profile (local)"]
    obs = db.get_observations(30)
    for o in obs[:20]:
        ts = datetime.fromtimestamp(o["ts"]).strftime("%m/%d %H:%M")
        lines.append(f"- [{o['type']}] {ts} {o['content'][:120]}")
    output = "\n".join(lines)
    _profile_cache.set("profile", output)
    return output[:4000]


async def memory_record_action(
    task: str, result: str, tool: str, success: bool = True
) -> str:
    """Record an action — instant local + async cloud."""
    ts = time.time()
    _insert_local_action(ts, task, result, tool, 1 if success else 0)
    _profile_cache.invalidate("profile")
    client = get_memory_client()
    if client:
        _bg(
            client.add_memory(
                f"[ACTION] {task} → {result[:200]} (tool: {tool}, success: {success})",
                "event",
            )
        )
    return "[action recorded]"


async def memory_record_conversation(role: str, content: str, context: str = "") -> str:
    """Record conversation turn — instant local + async session ingest (batched by AGI)."""
    ts = time.time()
    _insert_local_conversation(ts, role, content[:2000], context)
    _profile_cache.invalidate("profile")
    # Individual turns accumulate locally; AGI loop ingests as sessions periodically
    client = get_memory_client()
    if client:
        _bg(client.add_memory(f"[{role.upper()}]: {content[:500]}", "conversation"))
    return "[conversation recorded]"


async def memory_record_observation(
    content: str, obs_type: str = "fact", source: str = "screen"
) -> str:
    """Record observation with dedup — instant local + async extraction."""
    inserted = _insert_local_observation(obs_type, content, source)
    if inserted:
        _profile_cache.invalidate("profile")
        client = get_memory_client()
        if client:
            # Use extract endpoint so RetainDB types the memory properly
            _bg(client.extract_memory(content, context=f"source:{source}"))
    return "[observation recorded]" if inserted else "[already known]"


async def memory_store_file(file_path: str, scope: str = "USER") -> str:
    """Upload file to RetainDB — extracts searchable memories automatically."""
    client = get_memory_client()
    if not client:
        return "[error] RetainDB not configured"
    result = await client.upload_file(file_path, scope)
    if result.get("error"):
        return f"[error] {result['error']}"
    file_id = result.get("id") or result.get("file_id")
    if file_id:
        _bg(client.ingest_file(file_id))
    return f"[file stored] {result.get('path', file_path)} — ingestion started"


async def memory_list_files(prefix: str = "", scope: str = "USER") -> str:
    """List files stored in RetainDB."""
    client = get_memory_client()
    if not client:
        return "[error] RetainDB not configured"
    files = await client.list_files(prefix, scope)
    if not files:
        return "No files stored."
    lines = ["## Stored Files"]
    for f in files:
        lines.append(f"- {f.get('path', '?')} ({f.get('size', 0)} bytes)")
    return "\n".join(lines)


async def memory_forget(memory_id: str) -> str:
    """Delete a memory from RetainDB by ID."""
    client = get_memory_client()
    if not client:
        return "[error] RetainDB not configured"
    result = await client.delete_memory(memory_id)
    if result.get("error"):
        return f"[error] {result['error']}"
    return f"[deleted] memory {memory_id}"


async def memory_learn(fact: str, topic: str = "", confidence: float = 0.9) -> str:
    """Teach a verified fact directly via the learn endpoint."""
    client = get_memory_client()
    if not client:
        return await memory_add(fact, "factual")
    result = await client.learn(
        [
            {
                "content": fact,
                "topic": topic or "general",
                "confidence": confidence,
            }
        ]
    )
    if result.get("error"):
        return f"[error] {result['error']}"
    return f"[learned] {fact[:80]}"


async def memory_get_context(query: str, session_id: str = None) -> str:
    """
    Query RetainDB context endpoint — returns assembled prompt-ready context
    combining relevant memories, profile, and graph neighbors.
    """
    client = get_memory_client()
    if not client:
        return await memory_search(query)
    resp = await client.query_context(
        query, session_id=session_id, include_profile=True
    )
    context = resp.get("context") or resp.get("content") or ""
    if not context:
        return await memory_search(query)
    return context[:3000]


async def memory_get_usage() -> str:
    """Get RetainDB usage stats."""
    client = get_memory_client()
    if not client:
        return "[error] RetainDB not configured"
    usage = await client.get_usage()
    if usage.get("error"):
        return f"[error] {usage['error']}"
    lines = ["## RetainDB Usage"]
    for k, v in usage.items():
        if k != "error":
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)
