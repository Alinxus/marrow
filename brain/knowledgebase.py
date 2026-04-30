"""Unified knowledgebase surface for Marrow.

This module is the local retrieval boundary Marrow uses today. It merges
local SQLite memory, retained context, and reasoning-friendly summaries into
one interface that can later be swapped for the external agentic search infra
without changing the rest of the assistant.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import logging
import os
from typing import Any

from storage import db

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KnowledgeSource:
    key: str
    label: str
    mount_path: str
    description: str
    source_type: str = "local"
    read_only: bool = True


_SOURCES: tuple[KnowledgeSource, ...] = (
    KnowledgeSource(
        key="local.observations",
        label="Local Observations",
        mount_path="/knowledge/local/observations",
        description="Screen and ambient observations captured on this machine.",
    ),
    KnowledgeSource(
        key="local.conversations",
        label="Local Conversations",
        mount_path="/knowledge/local/conversations",
        description="Recent chat and assistant turns stored in SQLite.",
    ),
    KnowledgeSource(
        key="local.transcripts",
        label="Local Transcripts",
        mount_path="/knowledge/local/transcripts",
        description="Speech transcription fragments and audio-derived context.",
    ),
    KnowledgeSource(
        key="local.actions",
        label="Local Actions",
        mount_path="/knowledge/local/actions",
        description="Executed tool runs and outcomes.",
    ),
    KnowledgeSource(
        key="retained.memory",
        label="Retained Memory",
        mount_path="/knowledge/retained",
        description="RetainDB-backed long-term memory and profile context.",
        source_type="remote",
    ),
    KnowledgeSource(
        key="retained.graph",
        label="Retained Graph",
        mount_path="/knowledge/retained/graph",
        description="Cross-memory relationships and graph links.",
        source_type="remote",
    ),
)


def list_sources() -> list[dict[str, Any]]:
    """Return the built-in knowledge sources Marrow knows how to mount."""
    return [asdict(source) for source in _SOURCES]


def _db_counts() -> dict[str, int]:
    conn = db._connect()
    counts: dict[str, int] = {}
    for table in ("observations", "transcripts", "conversations", "actions"):
        try:
            counts[table] = int(
                conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
        except Exception:
            counts[table] = 0
    return counts


def status_snapshot() -> dict[str, Any]:
    """Return a compact runtime snapshot for the knowledge layer."""
    counts = _db_counts()
    available_sources = [s.key for s in _SOURCES]
    api_key_present = bool(os.environ.get("RETAINDB_API_KEY", ""))
    try:
        from actions.memory import get_memory_client

        retained = bool(get_memory_client())
    except Exception:
        retained = False
    if retained:
        retained_reason = "RetainDB client ready"
    elif not api_key_present:
        retained_reason = "RETAINDB_API_KEY is missing"
    else:
        retained_reason = "RetainDB client unavailable"
    return {
        "counts": counts,
        "source_count": len(_SOURCES),
        "available_sources": available_sources,
        "retained_available": retained,
        "retained_reason": retained_reason,
    }


def status_text() -> str:
    snap = status_snapshot()
    counts = snap.get("counts", {})
    lines = ["Knowledgebase status:"]
    lines.append(
        "- local="
        + ", ".join(
            [
                f"observations={counts.get('observations', 0)}",
                f"transcripts={counts.get('transcripts', 0)}",
                f"conversations={counts.get('conversations', 0)}",
                f"actions={counts.get('actions', 0)}",
            ]
        )
    )
    lines.append(
        f"- retained={'yes' if snap.get('retained_available') else 'no'} ({snap.get('retained_reason', 'unknown')})"
    )
    lines.append(f"- sources={snap.get('source_count', 0)}")
    for source in _SOURCES:
        lines.append(f"  - {source.key}: {source.mount_path}")
    return "\n".join(lines)


def _format_local_search(query: str, limit: int) -> str:
    query = (query or "").strip()
    if not query:
        return ""

    try:
        hits = db.search_all(query, limit=max(1, limit))
    except Exception as exc:
        log.debug(f"Knowledgebase local search failed: {exc}")
        return ""

    sections: list[str] = []
    for key, label in (
        ("observations", "Local observations"),
        ("conversations", "Local conversations"),
        ("actions", "Local actions"),
    ):
        rows = hits.get(key) or []
        if not rows:
            continue
        lines = [f"[{label}]"]
        for row in rows[:limit]:
            content = str(row.get("content") or row.get("task") or row.get("result") or "").strip()
            if not content:
                continue
            ts = row.get("ts")
            prefix = f"- {content[:220]}"
            if ts:
                prefix = f"- {content[:220]}"
            lines.append(prefix)
        if len(lines) > 1:
            sections.append("\n".join(lines))
    return "\n\n".join(sections)


async def build_context(
    query: str,
    *,
    session_id: str = "default",
    limit: int = 8,
) -> str:
    """Assemble the best local + retained context for a query."""
    parts: list[str] = []

    local = _format_local_search(query, limit=limit)
    if local:
        parts.append(local)

    try:
        from actions.memory import memory_get_context

        retained = await memory_get_context(query[:200], session_id=session_id)
    except Exception as exc:
        log.debug(f"Knowledgebase retained context failed: {exc}")
        retained = ""
    if retained:
        parts.append("[Retained memory]\n" + retained[:2200])

    if query:
        try:
            from brain.agi import get_agi

            oracle = await get_agi().get_oracle_context(query[:500], limit=limit)
        except Exception as exc:
            log.debug(f"Knowledgebase oracle context failed: {exc}")
            oracle = ""
        if oracle:
            parts.append(oracle[:2600])

    if not parts:
        try:
            obs = db.get_observations(limit=8)
        except Exception:
            obs = []
        if obs:
            lines = ["[Recent observations]"]
            for row in obs[:limit]:
                content = str(row.get("content") or "").strip()
                if content:
                    lines.append(f"- {content[:220]}")
            if len(lines) > 1:
                parts.append("\n".join(lines))

    return "\n\n".join(parts)


def build_mount_index() -> list[dict[str, Any]]:
    """Return a filesystem-like index of knowledge mounts."""
    return [
        {
            **asdict(source),
            "kind": source.source_type,
            "namespace": source.mount_path.split("/knowledge/", 1)[-1],
        }
        for source in _SOURCES
    ]
