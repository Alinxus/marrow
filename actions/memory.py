"""
Enhanced memory module for Marrow.

Architecture:
1. Local SQLite with FTS5 trigram index - instant reads/writes (~1ms)
2. RetainDB for cloud sync - background fire-and-forget
3. In-memory LRU cache - hot data at ~0ms
4. File storage - sync files to RetainDB for searchable memories

Speed: All reads hit local cache first. All writes go local instantly,
then async sync to RetainDB in background (never blocks main loop).

Features:
- User preferences (factual, preference, instruction)
- Conversation history (every turn)
- Actions taken (task → result → tool)
- Decisions/observations
- Screen context
- Files (PDF, text, etc - searchable memories)
- Temporal/numeric extraction for precision
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
from typing import Optional

import httpx

import config
from storage import db

log = logging.getLogger(__name__)

RETAINDB_API_KEY = os.environ.get("RETAINDB_API_KEY", "")
RETAINDB_PROJECT = os.environ.get("RETAINDB_PROJECT", "marrow")
RETAINDB_BASE_URL = "https://api.retaindb.com/v1"
MEMORY_USER_ID = "marrow_user"


# ─── LRU Cache for hot data ────────────────────────────────────────────────────


class MemoryCache:
    """LRU cache for recent memories - ~0ms access."""

    def __init__(self, max_size: int = 100, ttl: int = 30):
        self._cache = OrderedDict()
        self._ttl = ttl
        self._max_size = max_size

    def _hash_key(self, key: str) -> str:
        return hashlib.md5(key.encode()).hexdigest()

    def get(self, key: str) -> Optional[any]:
        h = self._hash_key(key)
        if h not in self._cache:
            return None
        item = self._cache[h]
        if time.time() - item["ts"] > self._ttl:
            del self._cache[h]
            return None
        self._cache.move_to_end(h)
        return item["value"]

    def set(self, key: str, value: any) -> None:
        h = self._hash_key(key)
        if h in self._cache:
            self._cache.move_to_end(h)
        self._cache[h] = {"value": value, "ts": time.time()}
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def invalidate(self, key: str) -> None:
        h = self._hash_key(key)
        if h in self._cache:
            del self._cache[h]


_memory_cache = MemoryCache(max_size=100, ttl=30)
_profile_cache = MemoryCache(max_size=20, ttl=60)


# ─── RetainDB HTTP Client ───────────────────────────────────────────────────────


class RetainDBClient:
    """HTTP client for RetainDB API - all operations async, fire-and-forget."""

    def __init__(self, api_key: str, project: str):
        self.api_key = api_key
        self.project = project
        self.base_url = RETAINDB_BASE_URL

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def add_memory(
        self, content: str, memory_type: str = "factual", session_id: str = None
    ) -> dict:
        """Add memory - fire-and-forget."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/memory",
                    headers=self._headers(),
                    json={
                        "project": self.project,
                        "user_id": MEMORY_USER_ID,
                        "session_id": session_id
                        or f"session_{datetime.now().timestamp()}",
                        "content": content,
                        "memory_type": memory_type,
                        "write_mode": "async",
                    },
                    timeout=5.0,
                )
                return resp.json()
        except Exception as e:
            log.debug(f"RetainDB add_memory failed: {e}")
            return {"error": str(e)}

    async def search_memory(self, query: str, limit: int = 10) -> list:
        """Search memories - falls back to local if RetainDB fails."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/memory/search",
                    headers=self._headers(),
                    json={
                        "project": self.project,
                        "user_id": MEMORY_USER_ID,
                        "query": query,
                        "limit": limit,
                        "include_pending": True,
                    },
                    timeout=10.0,
                )
                data = resp.json()
                return data.get("results", [])
        except Exception as e:
            log.debug(f"RetainDB search failed: {e}")
            return []

    async def store_file(self, file_path: str, scope: str = "USER") -> dict:
        """Store file and extract memories - via REST API."""
        try:
            async with httpx.AsyncClient() as client:
                with open(file_path, "rb") as f:
                    files = {
                        "file": (Path(file_path).name, f, "application/octet-stream")
                    }
                    data = {
                        "path": file_path,
                        "scope": scope,
                        "project_id": self.project,
                    }
                    resp = await client.post(
                        f"{self.base_url}/files",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        files=files,
                        data=data,
                        timeout=30.0,
                    )
                    return resp.json()
        except Exception as e:
            log.warning(f"RetainDB store_file failed: {e}")
            return {"error": str(e)}

    async def list_files(self, prefix: str = "", scope: str = "USER") -> list:
        """List stored files."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self.base_url}/files",
                    headers=self._headers(),
                    params={"prefix": prefix, "scope": scope, "limit": 50},
                    timeout=10.0,
                )
                data = resp.json()
                return data.get("files", [])
        except Exception as e:
            log.debug(f"RetainDB list_files failed: {e}")
            return []


_client: Optional[RetainDBClient] = None


def get_memory_client() -> Optional[RetainDBClient]:
    global _client
    if not RETAINDB_API_KEY:
        log.warning("RETAINDB_API_KEY not set - using local memory only")
        return None
    if _client is None:
        _client = RetainDBClient(RETAINDB_API_KEY, RETAINDB_PROJECT)
    return _client


# ─── Fast Local Operations (instant) ───────────────────────────────────────────


def _insert_local_conversation(
    ts: float, role: str, content: str, context: str = ""
) -> None:
    """Insert to local DB - instant (~1ms)."""
    try:
        db.insert_conversation(ts, role, content[:2000], context)
    except Exception as e:
        log.debug(f"Local conv insert error: {e}")


def _insert_local_action(
    ts: float, task: str, result: str, tool: str, success: int
) -> None:
    """Insert action to local DB - instant."""
    try:
        db.insert_action(ts, task, result[:500], tool, success)
    except Exception as e:
        log.debug(f"Local action insert error: {e}")


def _insert_local_observation(obs_type: str, content: str, source: str) -> bool:
    """Insert observation with dedup - returns True if new."""
    try:
        return db.insert_observation(obs_type, content, source)
    except Exception as e:
        log.debug(f"Local obs insert error: {e}")
        return False


# ─── Background Sync ────────────────────────────────────────────────────────────


def _bg_sync_to_retaindb(coro) -> None:
    """Fire-and-forget sync to RetainDB."""
    asyncio.create_task(coro)


# ─── Memory API ────────────────────────────────────────────────────────────────


async def memory_add(content: str, memory_type: str = "factual") -> str:
    """
    Add a memory - instant local, async cloud sync.
    """
    ts = time.time()

    # Instant local write
    _insert_local_conversation(ts, "memory", content, f"type:{memory_type}")

    # Invalidate cache
    _profile_cache.invalidate("profile")

    # Async sync to RetainDB (fire-and-forget)
    client = get_memory_client()
    if client:
        _bg_sync_to_retaindb(client.add_memory(content, memory_type))

    return f"[memory saved] {content[:50]}..."


async def memory_search(query: str) -> str:
    """
    Fast search - uses local FTS5 first (~5ms), falls back to RetainDB.
    """
    # Check cache first
    cached = _memory_cache.get(f"search:{query}")
    if cached:
        return cached

    results = []

    # Fast local FTS5 search (~5ms)
    convs = db.search_conversations(query, 5)
    results.extend([f"📝 [conversation] {c['content'][:150]}" for c in convs])

    acts = db.search_actions(query, 5)
    results.extend([f"⚡ [action] {a['task']}: {a['result'][:100]}" for a in acts])

    obs = db.get_observations_by_type(query, 5)
    results.extend([f"🔍 [observation] {o['content'][:150]}" for o in obs])

    # Fallback to RetainDB cloud
    if not results:
        client = get_memory_client()
        if client:
            cloud_results = await client.search_memory(query, 5)
            results.extend(
                [f"☁️ [cloud] {r.get('content', '')[:150]}" for r in cloud_results]
            )

    if not results:
        results = ["No memories found."]

    output = "\n".join(results)[:3000]

    # Cache result
    _memory_cache.set(f"search:{query}", output)

    return output


async def memory_get_profile() -> str:
    """
    Get all memory context - cached locally.
    """
    # Check cache
    cached = _profile_cache.get("profile")
    if cached:
        return cached

    profile = []

    # Get from local DB
    convs = db.get_recent_conversations(30)
    profile.extend(
        [
            {"type": "conversation", "content": c["content"], "ts": c["ts"]}
            for c in convs
        ]
    )

    acts = db.get_recent_actions(30)
    profile.extend(
        [
            {
                "type": "action",
                "content": f"{a['task']}: {a['result'][:100]}",
                "ts": a["ts"],
            }
            for a in acts
        ]
    )

    obs = db.get_observations(30)
    profile.extend(
        [{"type": "observation", "content": o["content"], "ts": o["ts"]} for o in obs]
    )

    # Sort by time
    profile.sort(key=lambda x: x.get("ts", 0), reverse=True)

    if not profile:
        output = "No memories yet."
    else:
        output_lines = ["## Memory Profile\n"]
        for p in profile[:30]:
            ts = (
                datetime.fromtimestamp(p["ts"]).strftime("%m/%d %H:%M")
                if p.get("ts")
                else ""
            )
            output_lines.append(
                f"- [{p.get('type', 'mem')[:4]}] {ts} {p['content'][:120]}"
            )
        output = "\n".join(output_lines)

    # Cache
    _profile_cache.set("profile", output)

    return output[:4000]


async def memory_record_action(
    task: str, result: str, tool: str, success: bool = True
) -> str:
    """
    Record an action Marrow took - instant local, async cloud sync.
    """
    ts = time.time()
    success_int = 1 if success else 0

    # Instant local
    _insert_local_action(ts, task, result, tool, success_int)

    # Invalidate cache
    _profile_cache.invalidate("profile")
    _memory_cache.invalidate(f"search:{task}")

    # Async sync
    client = get_memory_client()
    if client:
        _bg_sync_to_retaindb(
            client.add_memory(
                f"[ACTION] {task} → {result[:200]} (tool: {tool})", "event"
            )
        )

    return "[action recorded]"


async def memory_record_conversation(role: str, content: str, context: str = "") -> str:
    """
    Record conversation turn - instant local, async cloud sync.
    """
    ts = time.time()

    # Instant local
    _insert_local_conversation(ts, role, content[:2000], context)

    # Invalidate cache
    _profile_cache.invalidate("profile")

    # Async sync
    client = get_memory_client()
    if client:
        _bg_sync_to_retaindb(
            client.add_memory(
                f"[{role.upper()}]: {content[:500]}", "conversation", session_id=context
            )
        )

    return "[conversation recorded]"


async def memory_record_observation(
    content: str, obs_type: str = "fact", source: str = "screen"
) -> str:
    """
    Record observation - instant local with dedup, async cloud sync.
    """
    inserted = _insert_local_observation(obs_type, content, source)

    if inserted:
        _profile_cache.invalidate("profile")

        client = get_memory_client()
        if client:
            _bg_sync_to_retaindb(
                client.add_memory(f"[{source.upper()}] {content}", obs_type)
            )

    return "[observation recorded]" if inserted else "[already known]"


async def memory_store_file(file_path: str, scope: str = "USER") -> str:
    """
    Store file in RetainDB - extracts text and creates searchable memories.
    """
    client = get_memory_client()
    if not client:
        return "[error] RetainDB not configured"

    result = await client.store_file(file_path, scope)

    if "error" in result:
        return f"[error] {result['error']}"

    rdb_uri = result.get("rdb_uri", "")
    memories = result.get("memories_created", 0)

    return f"[file stored] {rdb_uri} ({memories} memories extracted)"


async def memory_list_files(prefix: str = "", scope: str = "USER") -> str:
    """List files stored in RetainDB."""
    client = get_memory_client()
    if not client:
        return "[error] RetainDB not configured"

    files = await client.list_files(prefix, scope)

    if not files:
        return "No files stored."

    output = ["## Stored Files\n"]
    for f in files:
        path = f.get("path", "")
        size = f.get("size", 0)
        output.append(f"- {path} ({size} bytes)")

    return "\n".join(output)


async def memory_forget(memory_id: str) -> str:
    """Delete a memory - local only for now."""
    return "[forget] Use RetainDB dashboard to delete cloud memories. Local: manual cleanup required."
