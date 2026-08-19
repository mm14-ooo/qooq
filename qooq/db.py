"""SQLite storage: schema, migrations, connection handling.

One save file = one story = one `.db`.  A save is self-contained: the world
bible lives in `meta` alongside the memory tables, so a story can be copied,
diffed or inspected with nothing but the `sqlite3` shell.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Verbatim transcript.  The prose the player actually read.
CREATE TABLE IF NOT EXISTS turns (
    n             INTEGER PRIMARY KEY,
    player_input  TEXT NOT NULL,
    narration     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);

-- Tier 1: who and what exists.  Small enough to inject in full every turn.
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,                    -- character|location|item|faction|concept
    name       TEXT NOT NULL UNIQUE,
    summary    TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'active',   -- active|absent|dead|destroyed|unknown
    importance INTEGER NOT NULL DEFAULT 2,       -- 1..5
    first_turn INTEGER NOT NULL,
    last_turn  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_aliases (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    PRIMARY KEY (entity_id, alias)
);

-- Append-only attributes.  A fact is never rewritten; it is superseded, so the
-- story keeps an audit trail of how a character changed.
CREATE TABLE IF NOT EXISTS facts (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    key           TEXT NOT NULL,
    value         TEXT NOT NULL,
    turn          INTEGER NOT NULL,
    superseded_by INTEGER REFERENCES facts(id)   -- NULL = currently true
);
CREATE INDEX IF NOT EXISTS facts_current ON facts(entity_id, key) WHERE superseded_by IS NULL;

CREATE TABLE IF NOT EXISTS relations (
    id            INTEGER PRIMARY KEY,
    src_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    dst_id        INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    strength      INTEGER NOT NULL DEFAULT 0,    -- -3 hostile .. +3 devoted
    turn          INTEGER NOT NULL,
    superseded_by INTEGER REFERENCES relations(id)
);
CREATE INDEX IF NOT EXISTS relations_current ON relations(src_id, dst_id, kind)
    WHERE superseded_by IS NULL;

-- Plot state the narrator must respect: quest stages, secrets revealed, doors opened.
CREATE TABLE IF NOT EXISTS flags (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    turn  INTEGER NOT NULL
);

-- Tier 2: episodic memory.  Too large to inject in full; retrieved by relevance.
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY,
    turn              INTEGER NOT NULL,
    summary           TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'event',
    importance        INTEGER NOT NULL DEFAULT 2,   -- 1..5
    location          TEXT NOT NULL DEFAULT '',
    consolidated_into INTEGER REFERENCES chapters(id)
);
CREATE INDEX IF NOT EXISTS events_turn ON events(turn);

CREATE TABLE IF NOT EXISTS event_participants (
    event_id  INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX IF NOT EXISTS event_participants_entity ON event_participants(entity_id);

-- Pre-tokenised mirror of events.summary (see qooq.textutil for why).
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    search_text,
    tokenize = 'unicode61'
);

-- Tier 3: consolidated history.  Bounds context growth over a long story.
CREATE TABLE IF NOT EXISTS chapters (
    id         INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    from_turn  INTEGER NOT NULL,
    to_turn    INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) a save file with the schema applied."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    cur = get_meta(conn, "schema_version")
    if cur is None:
        set_meta(conn, "schema_version", SCHEMA_VERSION)
    elif int(cur) > SCHEMA_VERSION:
        raise RuntimeError(
            f"save file was written by a newer qooq (schema {cur} > {SCHEMA_VERSION})"
        )
    conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )
