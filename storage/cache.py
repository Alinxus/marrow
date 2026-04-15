"""
Fast caching layer for Marrow.

Uses:
- In-memory LRU cache for hot data
- SQLite with FTS5 trigram for search
- Precomputed embeddings for quick similarity

Speed optimizations:
1. LRU cache for recent context (no DB hit)
2. Pre-fetch context in background
3. Batch inserts for high-frequency data
"""

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from datetime import datetime
from typing import Optional, Any

import config

log = logging.getLogger(__name__)


class FastCache:
    """LRU cache with TTL for hot data."""

    def __init__(self, max_size: int = 100, ttl: int = 30):
        self._cache = OrderedDict()
        self._ttl = ttl
        self._max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        if key not in self._cache:
            return None

        item = self._cache[key]
        if time.time() - item["ts"] > self._ttl:
            del self._cache[key]
            return None

        # Move to end (most recently used)
        self._cache.move_to_end(key)
        return item["value"]

    def set(self, key: str, value: Any) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = {"value": value, "ts": time.time()}

        # Evict oldest
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def invalidate(self, key: str) -> None:
        if key in self._cache:
            del self._cache[key]

    def clear(self) -> None:
        self._cache.clear()


# Global caches
_context_cache = FastCache(max_size=50, ttl=10)  # Recent context - 10s TTL
_screen_cache = FastCache(max_size=20, ttl=3)  # Recent screens - 3s TTL
_action_cache = FastCache(max_size=100, ttl=60)  # Recent actions - 60s TTL


def get_cached_context(seconds: int = 60) -> dict:
    """Get recent context with caching."""
    key = f"context:{seconds}"

    cached = _context_cache.get(key)
    if cached is not None:
        log.debug("Using cached context")
        return cached

    # Fetch from DB
    from storage import db

    context = db.get_recent_context(seconds)

    _context_cache.set(key, context)
    return context


def cache_invalidate_context() -> None:
    """Invalidate context cache."""
    _context_cache.clear()


def get_cached_screenshots(seconds: int = 30) -> list:
    """Get recent screenshots with caching."""
    key = f"screens:{seconds}"

    cached = _screen_cache.get(key)
    if cached is not None:
        return cached

    from storage import db

    ctx = db.get_recent_context(seconds)
    screens = ctx.get("screenshots", [])

    _screen_cache.set(key, screens)
    return screens


def get_cached_actions(limit: int = 10) -> list:
    """Get recent actions with caching."""
    key = f"actions:{limit}"

    cached = _action_cache.get(key)
    if cached is not None:
        return cached

    from storage import db

    actions = db.get_recent_actions(limit)

    _action_cache.set(key, actions)
    return actions


async def prefetch_context():
    """Pre-fetch context in background."""
    asyncio.create_task(_prefetch_loop())


async def _prefetch_loop():
    """Background context prefetch."""
    while True:
        try:
            # Pre-fetch context
            from storage import db

            ctx = db.get_recent_context(60)
            _context_cache.set("context:60", ctx)

            # Pre-fetch recent actions
            acts = db.get_recent_actions(20)
            _action_cache.set("actions:20", acts)

        except Exception as e:
            log.debug(f"Prefetch error: {e}")

        await asyncio.sleep(5)  # Prefetch every 5 seconds


class QueryCache:
    """Cache for frequently queried data."""

    def __init__(self):
        self._queries = {}

    def hash_query(self, q: str, params: dict = None) -> str:
        data = f"{q}:{json.dumps(params or {})}"
        return hashlib.md5(data.encode()).hexdigest()

    def get(self, query: str, params: dict = None) -> Optional[dict]:
        key = self.hash_query(query, params)
        return self._queries.get(key)

    def set(self, query: str, params: dict, value: dict) -> None:
        key = self.hash_query(query, params)
        self._queries[key] = value


_query_cache = QueryCache()


def cached_search(query: str, max_results: int = 10) -> dict:
    """Fast cached search."""
    key = f"search:{query}:{max_results}"

    cached = _context_cache.get(key)
    if cached:
        return cached

    from storage import db

    results = db.search_all(query, max_results)

    _context_cache.set(key, results)
    return results


def build_context_prompt() -> str:
    """Build context prompt from cached data."""
    from storage import db

    ctx = db.get_recent_context(config.CONTEXT_WINDOW_SECONDS)

    if not ctx.get("screens") and not ctx.get("transcripts"):
        return ""

    lines = ["## Recent Context"]

    # Recent screens
    for s in ctx.get("screenshots", [])[:5]:
        app = s.get("app_name", "?")
        title = s.get("window_title", "")[:50]
        focused = s.get("focused_context", "")
        ocr = s.get("ocr_text", "")[:200]
        lines.append(f"[{app}] {title}")
        if focused:
            lines.append(f"  focused: {focused}")
        if ocr:
            lines.append(f"  visible: {ocr}")

    # Recent transcripts
    for t in ctx.get("transcripts", [])[:3]:
        text = t.get("text", "")[:150]
        if text:
            lines.append(f"[speech] {text}")

    return "\n".join(lines)
