"""
SQLite storage layer with FTS5 full-text search.

Tables
------
memories          -- canonical memory records
memories_fts      -- FTS5 virtual table mirroring memories.content
episodic_sessions -- session logs with causal links
quarantine        -- low-trust writes pending human review
skills            -- procedural memory (workflows, playbooks)
"""
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

from config.settings import settings


DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    memory_type TEXT NOT NULL CHECK(memory_type IN ('semantic','episodic','procedural','relational')),
    content     TEXT NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',   -- JSON blob
    trust_score REAL NOT NULL DEFAULT 1.0,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    accessed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    decayed_score REAL NOT NULL DEFAULT 1.0,
    owner       TEXT NOT NULL DEFAULT 'shared' -- 'shared' or user id
);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED,
    content,
    content='memories',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, id, content) VALUES (new.rowid, new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, id, content) VALUES('delete', old.rowid, old.id, old.content);
    INSERT INTO memories_fts(rowid, id, content) VALUES (new.rowid, new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, id, content) VALUES('delete', old.rowid, old.id, old.content);
END;

CREATE TABLE IF NOT EXISTS episodic_sessions (
    id           TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    summary      TEXT NOT NULL,
    memory_ids   TEXT NOT NULL DEFAULT '[]',  -- JSON array of memory IDs referenced
    caused_by    TEXT,                         -- parent session ID (causal chain)
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);

CREATE TABLE IF NOT EXISTS quarantine (
    id           TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    content      TEXT NOT NULL,
    metadata     TEXT NOT NULL DEFAULT '{}',
    trust_score  REAL NOT NULL,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    reviewed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skills (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT NOT NULL,
    template      TEXT NOT NULL,            -- workflow template / prompt
    use_count     INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    level         TEXT NOT NULL DEFAULT 'draft'  -- draft | active | crystallized
        CHECK(level IN ('draft','active','crystallized')),
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_type    ON memories(memory_type);
CREATE INDEX IF NOT EXISTS idx_memories_agent   ON memories(agent_id);
CREATE INDEX IF NOT EXISTS idx_memories_owner   ON memories(owner);
CREATE INDEX IF NOT EXISTS idx_memories_decayed ON memories(decayed_score DESC);
"""


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.executescript(DDL)
    conn.commit()
    conn.close()


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
