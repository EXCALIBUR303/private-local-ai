"""
SQLite storage for conversations, indexed documents, reminders and the
action audit log.

One file, in the local data directory, holding everything. Deleting that
file deletes every trace of the application's state.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from . import config

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT    NOT NULL DEFAULT 'New chat',
    model       TEXT,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT    NOT NULL CHECK (role IN ('user','assistant','system')),
    content         TEXT    NOT NULL,
    model           TEXT,
    citations       TEXT,          -- JSON array, assistant messages only
    created_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT    NOT NULL UNIQUE,   -- absolute path on this Mac
    filename     TEXT    NOT NULL,
    ext          TEXT    NOT NULL,
    bytes        INTEGER NOT NULL,
    sha256       TEXT    NOT NULL,
    origin       TEXT    NOT NULL,          -- 'folder' | 'upload'
    chunk_count  INTEGER NOT NULL DEFAULT 0,
    embed_model  TEXT,
    added_at     REAL    NOT NULL,
    indexed_at   REAL
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id  INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    locator    TEXT,               -- human-readable: "page 4", "Section: Intro"
    page       INTEGER,
    section    TEXT,
    embedding  BLOB,               -- float32 little-endian, L2-normalised
    dim        INTEGER,
    created_at REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id, ordinal);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL,
    due        TEXT,
    status     TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL,
    params      TEXT    NOT NULL,   -- JSON
    approved    INTEGER NOT NULL,   -- 0/1
    outcome     TEXT,
    origin      TEXT,               -- 'user' | 'model-proposal'
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    """Thread-local connection. FastAPI runs sync handlers in a threadpool."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.ensure_dirs()
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    config.ensure_dirs()
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def close_thread_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ---------------------------------------------------------------- conversations
def create_conversation(title: str = "New chat", model: str | None = None) -> int:
    now = time.time()
    with tx() as c:
        cur = c.execute(
            "INSERT INTO conversations (title, model, created_at, updated_at)"
            " VALUES (?,?,?,?)",
            (title, model, now, now),
        )
        return int(cur.lastrowid)


def list_conversations(limit: int = 200) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id)"
        " AS message_count FROM conversations c ORDER BY c.updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_messages(conversation_id: int) -> list[dict[str, Any]]:
    rows = connect().execute(
        "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["citations"] = json.loads(d["citations"]) if d["citations"] else []
        out.append(d)
    return out


def add_message(
    conversation_id: int,
    role: str,
    content: str,
    model: str | None = None,
    citations: list[dict[str, Any]] | None = None,
) -> int:
    now = time.time()
    with tx() as c:
        cur = c.execute(
            "INSERT INTO messages (conversation_id, role, content, model, citations,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (
                conversation_id,
                role,
                content,
                model,
                json.dumps(citations) if citations else None,
                now,
            ),
        )
        c.execute(
            "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
        )
        return int(cur.lastrowid)


def rename_conversation(conversation_id: int, title: str) -> None:
    with tx() as c:
        c.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
            (title[:120], time.time(), conversation_id),
        )


def delete_conversation(conversation_id: int) -> None:
    with tx() as c:
        c.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))


# --------------------------------------------------------------------- settings
def get_setting(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO settings (key,value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


# -------------------------------------------------------------------- wipe-all
def delete_all_data() -> dict[str, int]:
    """Truncate every table. Returns per-table row counts removed."""
    conn = connect()
    counts: dict[str, int] = {}
    # Modules create their own tables at startup (memories, voices), so the
    # wipe asks the database what actually exists rather than assuming. That
    # keeps it correct in tests, on a fresh install, and for anything added
    # later — a wipe that silently skipped a table would be a privacy bug.
    wanted = [
        "messages",
        "conversations",
        "memories",
        "voices",
        "chunks",
        "sources",
        "reminders",
        "action_log",
        "watch_events",
        "watchers",
        "egress_log",
        "secrets",
        "settings",
    ]
    present = {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    with tx() as c:
        for t in wanted:
            if t not in present:
                continue
            counts[t] = c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            c.execute(f"DELETE FROM {t}")
        if "sqlite_sequence" in present:
            c.execute("DELETE FROM sqlite_sequence")
    conn.execute("VACUUM")
    return counts
