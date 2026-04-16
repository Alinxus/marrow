"""
Local SQLite store for Marrow.
Tables:
  - screenshots   : timestamped screen captures with semantic OCR
  - transcripts   : audio transcription chunks
  - observations  : durable world model facts (deduped)
  - interruptions : history of what Marrow said (for cooldown/dedup)

Design decisions:
  - Thread-local connections: SQLite connections are not thread-safe.
    Each thread gets its own connection so capture loop, audio loop,
    and reasoning loop can all write without locking each other.
  - WAL mode: allows concurrent reads + one writer; much better for
    an always-on background process.
  - Observations are deduped by (type, content) hash so the world
    model doesn't bloat with repeated facts.
"""

import hashlib
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".marrow" / "marrow.db"

# Thread-local storage — each thread gets its own connection
_local = threading.local()


def _connect() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS screenshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               REAL    NOT NULL,
            app_name         TEXT,
            window_title     TEXT,
            focused_context  TEXT,
            ocr_text         TEXT,
            image_path       TEXT,
            content_hash     TEXT    -- dedup: skip unchanged screens
        );

        CREATE TABLE IF NOT EXISTS transcripts (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            ts   REAL NOT NULL,
            text TEXT NOT NULL,
            speaker TEXT,
            embedding BLOB
        );

        CREATE TABLE IF NOT EXISTS observations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            type         TEXT NOT NULL,
            content      TEXT NOT NULL,
            source       TEXT,
            content_hash TEXT UNIQUE  -- dedup: same fact never stored twice
        );

        CREATE TABLE IF NOT EXISTS interruptions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            message    TEXT NOT NULL,
            reasoning  TEXT,
            urgency    INTEGER DEFAULT 3,
            was_spoken INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS todos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            title       TEXT NOT NULL,
            description TEXT,
            due_ts      REAL,
            priority    INTEGER DEFAULT 3,
            tags        TEXT,
            status      TEXT DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            trigger_ts REAL NOT NULL,
            message    TEXT NOT NULL,
            action     TEXT,
            status     TEXT DEFAULT 'pending'
        );

        -- Action history for memory
        CREATE TABLE IF NOT EXISTS actions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            task        TEXT NOT NULL,
            result      TEXT,
            tool_used   TEXT,
            success     INTEGER DEFAULT 1
        );

        -- Conversation history for memory
        CREATE TABLE IF NOT EXISTS conversations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         REAL NOT NULL,
            role       TEXT NOT NULL,  -- user, assistant
            content    TEXT NOT NULL,
            context    TEXT  -- what triggered this
        );

        CREATE TABLE IF NOT EXISTS contact_interactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            contact     TEXT NOT NULL,
            channel     TEXT NOT NULL,
            direction   TEXT NOT NULL,  -- outgoing|incoming
            action      TEXT NOT NULL,  -- sent|reply|draft|received
            source_app  TEXT,
            evidence    TEXT,
            confidence  REAL DEFAULT 0.5
        );

        CREATE TABLE IF NOT EXISTS claim_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            topic       TEXT NOT NULL,
            claim       TEXT NOT NULL,
            verdict     TEXT,
            source_app  TEXT,
            evidence    TEXT,
            confidence  REAL DEFAULT 0.5
        );

        -- FTS5 trigram indexes for fast search
        CREATE VIRTUAL TABLE IF NOT EXISTS obs_fts USING fts5(
            content, type, source,
            content='observations',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS conv_fts USING fts5(
            content, role,
            content='conversations',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS action_fts USING fts5(
            task, result, tool_used,
            content='actions',
            content_rowid='id',
            tokenize='trigram'
        );

        CREATE INDEX IF NOT EXISTS idx_screenshots_ts    ON screenshots(ts);
        CREATE INDEX IF NOT EXISTS idx_transcripts_ts    ON transcripts(ts);
        CREATE INDEX IF NOT EXISTS idx_interruptions_ts  ON interruptions(ts);
        CREATE INDEX IF NOT EXISTS idx_observations_type ON observations(type);
        CREATE INDEX IF NOT EXISTS idx_todos_status      ON todos(status);
        CREATE INDEX IF NOT EXISTS idx_reminders_trigger ON reminders(trigger_ts);
        CREATE INDEX IF NOT EXISTS idx_actions_ts        ON actions(ts);
        CREATE INDEX IF NOT EXISTS idx_conversations_ts  ON conversations(ts);
        CREATE INDEX IF NOT EXISTS idx_contact_ts        ON contact_interactions(ts);
        CREATE INDEX IF NOT EXISTS idx_contact_name      ON contact_interactions(contact);
        CREATE INDEX IF NOT EXISTS idx_claim_ts          ON claim_events(ts);
    """)
    conn.commit()


# ─── Writes ────────────────────────────────────────────────────────────────────


def insert_screenshot(
    ts: float,
    app_name: str,
    window_title: str,
    focused_context: str,
    ocr_text: str,
    image_path: str = "",
    content_hash: str = "",
) -> None:
    conn = _connect()
    conn.execute(
        """INSERT INTO screenshots
           (ts, app_name, window_title, focused_context, ocr_text, image_path, content_hash)
           VALUES (?,?,?,?,?,?,?)""",
        (
            ts,
            app_name,
            window_title,
            focused_context,
            ocr_text,
            image_path,
            content_hash,
        ),
    )
    conn.commit()


def insert_transcript(ts: float, text: str) -> None:
    conn = _connect()
    conn.execute("INSERT INTO transcripts (ts, text) VALUES (?,?)", (ts, text))
    conn.commit()


def insert_interruption(
    ts: float, message: str, reasoning: str = "", urgency: int = 3
) -> None:
    conn = _connect()
    conn.execute(
        "INSERT INTO interruptions (ts, message, reasoning, urgency) VALUES (?,?,?,?)",
        (ts, message, reasoning, urgency),
    )
    conn.commit()


def insert_observation(type_: str, content: str, source: str = "screen") -> bool:
    """Insert observation if not already known. Returns True if actually inserted."""
    h = hashlib.sha256(f"{type_}:{content}".encode()).hexdigest()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO observations (ts, type, content, source, content_hash) VALUES (?,?,?,?,?)",
            (datetime.utcnow().timestamp(), type_, content, source, h),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Already exists (UNIQUE constraint on content_hash)
        return False


# ─── Reads ─────────────────────────────────────────────────────────────────────


def get_recent_context(window_seconds: int) -> dict:
    """Pull screenshots + transcripts from the last N seconds."""
    conn = _connect()
    cutoff = (datetime.utcnow() - timedelta(seconds=window_seconds)).timestamp()

    screenshots = conn.execute(
        """SELECT ts, app_name, window_title, focused_context, ocr_text
           FROM screenshots WHERE ts > ? ORDER BY ts DESC LIMIT 20""",
        (cutoff,),
    ).fetchall()

    transcripts = conn.execute(
        "SELECT ts, text FROM transcripts WHERE ts > ? ORDER BY ts ASC",
        (cutoff,),
    ).fetchall()

    return {
        "screenshots": [dict(r) for r in screenshots],
        "transcripts": [dict(r) for r in transcripts],
    }


def get_last_screenshot() -> Optional[dict]:
    """Return the most recent screenshot row (for dedup)."""
    conn = _connect()
    row = conn.execute(
        "SELECT app_name, window_title, content_hash FROM screenshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_recent_interruptions(window_seconds: int) -> list:
    conn = _connect()
    cutoff = (datetime.utcnow() - timedelta(seconds=window_seconds)).timestamp()
    rows = conn.execute(
        "SELECT ts, message, urgency FROM interruptions WHERE ts > ? ORDER BY ts DESC",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_observations(limit: int = 50) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT type, content, ts FROM observations ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_observations_since_id(last_id: int, limit: int = 50) -> list:
    """Get observations with id > last_id (for wiki incremental updates)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT id, type, content, ts FROM observations WHERE id > ? ORDER BY id ASC LIMIT ?",
        (last_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_transcripts(window_seconds: int = 3600) -> list:
    """Get recent audio transcripts."""
    conn = _connect()
    cutoff = (datetime.utcnow() - timedelta(seconds=window_seconds)).timestamp()
    rows = conn.execute(
        "SELECT ts, text FROM transcripts WHERE ts > ? ORDER BY ts DESC LIMIT 30",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_observations_by_type(type_: str, limit: int = 20) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT content, ts FROM observations WHERE type=? ORDER BY ts DESC LIMIT ?",
        (type_, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_apps(window_seconds: int = 300) -> list[str]:
    """Return distinct app names seen in the last N seconds."""
    conn = _connect()
    cutoff = (datetime.utcnow() - timedelta(seconds=window_seconds)).timestamp()
    rows = conn.execute(
        "SELECT DISTINCT app_name FROM screenshots WHERE ts > ? AND app_name IS NOT NULL",
        (cutoff,),
    ).fetchall()
    return [r["app_name"].lower() for r in rows if r["app_name"]]


def prune_old_data(days: int = 7) -> None:
    """Delete screenshot rows older than N days. Keep observations forever."""
    conn = _connect()
    cutoff = (datetime.utcnow() - timedelta(days=days)).timestamp()
    conn.execute("DELETE FROM screenshots WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM transcripts WHERE ts < ?", (cutoff,))
    conn.execute(
        "DELETE FROM interruptions WHERE ts < ?",
        ((datetime.utcnow() - timedelta(days=30)).timestamp(),),
    )
    conn.commit()


# ─── Todos ──────────────────────────────────────────────────────────────────────


def insert_todo(
    ts: float,
    title: str,
    description: str = "",
    due_ts: float = None,
    priority: int = 3,
    tags: str = "[]",
    status: str = "pending",
) -> int:
    conn = _connect()
    cursor = conn.execute(
        """INSERT INTO todos (ts, title, description, due_ts, priority, tags, status)
           VALUES (?,?,?,?,?,?,?)""",
        (ts, title, description, due_ts, priority, tags, status),
    )
    conn.commit()
    return cursor.lastrowid


def get_todos(status: str = "pending", limit: int = 20) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM todos WHERE status = ? ORDER BY due_ts, priority DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def update_todo_status(todo_id: int, status: str) -> None:
    conn = _connect()
    conn.execute("UPDATE todos SET status = ? WHERE id = ?", (status, todo_id))
    conn.commit()


def delete_todo(todo_id: int) -> None:
    conn = _connect()
    conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    conn.commit()


def search_todos(query: str) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM todos WHERE title LIKE ? OR description LIKE ? ORDER BY ts DESC LIMIT 20",
        (f"%{query}%", f"%{query}%"),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Reminders ────────────────────────────────────────────────────────────────


def insert_reminder(
    ts: float,
    trigger_ts: float,
    message: str,
    action: str = None,
    status: str = "pending",
) -> int:
    conn = _connect()
    cursor = conn.execute(
        """INSERT INTO reminders (ts, trigger_ts, message, action, status)
           VALUES (?,?,?,?,?)""",
        (ts, trigger_ts, message, action, status),
    )
    conn.commit()
    return cursor.lastrowid


def get_pending_reminders() -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE status = 'pending' ORDER BY trigger_ts",
    ).fetchall()
    return [dict(r) for r in rows]


def get_due_reminders(now: float) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM reminders WHERE status = 'pending' AND trigger_ts <= ?",
        (now,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_reminder_status(reminder_id: int, status: str) -> None:
    conn = _connect()
    conn.execute("UPDATE reminders SET status = ? WHERE id = ?", (status, reminder_id))
    conn.commit()


# ─── Actions history ────────────────────────────────────────────────────────────


def insert_action(
    ts: float, task: str, result: str = "", tool_used: str = "", success: int = 1
) -> int:
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO actions (ts, task, result, tool_used, success) VALUES (?,?,?,?,?)",
        (ts, task, result, tool_used, success),
    )
    conn.commit()
    # Update FTS
    conn.execute(
        "INSERT INTO action_fts (rowid, task, result, tool_used) VALUES (?,?,?,?)",
        (cursor.lastrowid, task, result, tool_used),
    )
    conn.commit()
    return cursor.lastrowid


def get_recent_actions(limit: int = 50) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM actions ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def search_actions(query: str, limit: int = 20) -> list:
    """Fast FTS5 search on actions."""
    conn = _connect()
    rows = conn.execute(
        "SELECT actions.* FROM action_fts JOIN actions ON action_fts.rowid = actions.id WHERE action_fts MATCH ? LIMIT ?",
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Conversations history ────────────────────────────────────────────────────────


def insert_conversation(ts: float, role: str, content: str, context: str = "") -> int:
    conn = _connect()
    cursor = conn.execute(
        "INSERT INTO conversations (ts, role, content, context) VALUES (?,?,?,?)",
        (ts, role, content, context),
    )
    conn.commit()
    # Update FTS
    conn.execute(
        "INSERT INTO conv_fts (rowid, content, role) VALUES (?,?,?)",
        (cursor.lastrowid, content, role),
    )
    conn.commit()
    return cursor.lastrowid


def get_recent_conversations(limit: int = 50) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM conversations ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def search_conversations(query: str, limit: int = 20) -> list:
    """Fast FTS5 search on conversations."""
    conn = _connect()
    rows = conn.execute(
        "SELECT conversations.* FROM conv_fts JOIN conversations ON conv_fts.rowid = conversations.id WHERE conv_fts MATCH ? LIMIT ?",
        (query, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Fast search across all memory ────────────────────────────────────────────────


def search_all(query: str, limit: int = 10) -> dict:
    """Search across observations, actions, and conversations."""
    conn = _connect()

    results = {
        "observations": [],
        "actions": [],
        "conversations": [],
    }

    # Search observations
    try:
        rows = conn.execute(
            "SELECT * FROM observations_fts WHERE observations_fts MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
        results["observations"] = [dict(r) for r in rows]
    except:
        pass

    # Search actions
    try:
        rows = conn.execute(
            "SELECT * FROM action_fts WHERE action_fts MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
        results["actions"] = [dict(r) for r in rows]
    except:
        pass

    # Search conversations
    try:
        rows = conn.execute(
            "SELECT * FROM conv_fts WHERE conv_fts MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
        results["conversations"] = [dict(r) for r in rows]
    except:
        pass

    return results


# ─── High-context awareness tables ─────────────────────────────────────────────


def insert_contact_interaction(
    ts: float,
    contact: str,
    channel: str,
    direction: str,
    action: str,
    source_app: str = "",
    evidence: str = "",
    confidence: float = 0.5,
) -> bool:
    """Insert deduped contact interaction. Returns True if inserted."""
    conn = _connect()

    recent = conn.execute(
        """SELECT id FROM contact_interactions
           WHERE contact = ? AND channel = ? AND action = ? AND ts > ?
           ORDER BY ts DESC LIMIT 1""",
        (contact.lower(), channel, action, ts - 120),
    ).fetchone()
    if recent:
        return False

    conn.execute(
        """INSERT INTO contact_interactions
           (ts, contact, channel, direction, action, source_app, evidence, confidence)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            ts,
            contact.lower(),
            channel,
            direction,
            action,
            source_app,
            evidence,
            confidence,
        ),
    )
    conn.commit()
    return True


def get_contact_pressure_signals(window_days: int = 14, limit: int = 10) -> list:
    """
    Contacts with high outgoing-to-incoming ratio.
    Useful for: "you've emailed them 3 times with no response" style prompts.
    """
    conn = _connect()
    cutoff = (datetime.utcnow() - timedelta(days=window_days)).timestamp()
    rows = conn.execute(
        """SELECT
               contact,
               SUM(CASE WHEN direction = 'outgoing' THEN 1 ELSE 0 END) AS outgoing,
               SUM(CASE WHEN direction = 'incoming' THEN 1 ELSE 0 END) AS incoming,
               MAX(ts) AS last_ts
           FROM contact_interactions
           WHERE ts > ?
           GROUP BY contact
           HAVING outgoing >= 2
           ORDER BY (outgoing - incoming) DESC, last_ts DESC
           LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_contact_interactions(limit: int = 25) -> list:
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM contact_interactions ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_claim_event(
    ts: float,
    topic: str,
    claim: str,
    verdict: str = "",
    source_app: str = "",
    evidence: str = "",
    confidence: float = 0.5,
) -> bool:
    """Insert deduped claim event. Returns True if inserted."""
    conn = _connect()
    recent = conn.execute(
        """SELECT id FROM claim_events
           WHERE ts > ? AND topic = ? AND claim = ?
           ORDER BY ts DESC LIMIT 1""",
        (ts - 600, topic.lower(), claim[:500]),
    ).fetchone()
    if recent:
        return False

    conn.execute(
        """INSERT INTO claim_events
           (ts, topic, claim, verdict, source_app, evidence, confidence)
           VALUES (?,?,?,?,?,?,?)""",
        (
            ts,
            topic.lower(),
            claim[:500],
            verdict,
            source_app,
            evidence[:600],
            confidence,
        ),
    )
    conn.commit()
    return True


def get_recent_claim_events(window_hours: int = 24, limit: int = 20) -> list:
    conn = _connect()
    cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).timestamp()
    rows = conn.execute(
        """SELECT * FROM claim_events
           WHERE ts > ?
           ORDER BY ts DESC LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]
