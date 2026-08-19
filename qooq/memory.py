"""The memory store.

Three tiers of memory, each with a different job:

* **State** (`entities`, `facts`, `relations`, `flags`) — what is true *now*.
  Small and stable, so it is injected into every prompt in full.
* **Episodic** (`events`) — what *happened*.  Grows without bound, so it is
  injected by relevance: see :func:`MemoryStore.retrieve`.
* **Chapters** — old episodic memory compressed into prose.  Keeps the prompt
  bounded no matter how long the story runs.

Facts and relations are append-only: a change supersedes the old row rather
than overwriting it, so "what did she believe in chapter two?" stays answerable.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence

from . import textutil
from .config import RetrievalWeights

ENTITY_KINDS = ("character", "location", "item", "faction", "concept")
ENTITY_STATUSES = ("active", "absent", "dead", "destroyed", "unknown")


@dataclass
class RetrievedEvent:
    id: int
    turn: int
    summary: str
    importance: int
    location: str
    participants: list[str]
    score: float
    parts: dict[str, float] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


class MemoryStore:
    def __init__(self, conn: sqlite3.Connection, weights: RetrievalWeights | None = None):
        self.conn = conn
        self.weights = weights or RetrievalWeights()

    # ---------------------------------------------------------------- turns

    @property
    def turn(self) -> int:
        """The number of turns already played."""
        row = self.conn.execute("SELECT COALESCE(MAX(n), 0) AS n FROM turns").fetchone()
        return int(row["n"])

    def record_turn(
        self,
        n: int,
        player_input: str,
        narration: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO turns(n, player_input, narration, created_at,"
            " input_tokens, output_tokens) VALUES(?, ?, ?, ?, ?, ?)",
            (n, player_input, narration, _now(), input_tokens, output_tokens),
        )

    def recent_turns(self, limit: int) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM turns ORDER BY n DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed(rows))

    # ------------------------------------------------------------- entities

    def resolve_entity(self, name: str) -> sqlite3.Row | None:
        """Find an entity by canonical name or alias, case-insensitively."""
        name = (name or "").strip()
        if not name:
            return None
        row = self.conn.execute(
            "SELECT * FROM entities WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row is not None:
            return row
        return self.conn.execute(
            "SELECT e.* FROM entities e JOIN entity_aliases a ON a.entity_id = e.id"
            " WHERE a.alias = ? COLLATE NOCASE",
            (name,),
        ).fetchone()

    def upsert_entity(
        self,
        name: str,
        kind: str = "concept",
        summary: str = "",
        status: str = "",
        importance: int | None = None,
        aliases: Sequence[str] = (),
        turn: int | None = None,
    ) -> int:
        """Create an entity, or update the fields the caller actually supplied."""
        name = name.strip()
        if not name:
            raise ValueError("entity name must not be empty")
        turn = self.turn if turn is None else turn
        kind = kind if kind in ENTITY_KINDS else "concept"

        existing = self.resolve_entity(name)
        if existing is None:
            cur = self.conn.execute(
                "INSERT INTO entities(kind, name, summary, status, importance,"
                " first_turn, last_turn) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    name,
                    summary,
                    status or "active",
                    _clamp(importance if importance is not None else 2, 1, 5),
                    turn,
                    turn,
                ),
            )
            entity_id = int(cur.lastrowid)
        else:
            entity_id = int(existing["id"])
            sets, params = ["last_turn = ?"], [turn]
            if summary:
                sets.append("summary = ?")
                params.append(summary)
            if status:
                sets.append("status = ?")
                params.append(status)
            if importance is not None:
                sets.append("importance = ?")
                params.append(_clamp(importance, 1, 5))
            if kind != "concept" and existing["kind"] == "concept":
                sets.append("kind = ?")
                params.append(kind)
            params.append(entity_id)
            self.conn.execute(
                f"UPDATE entities SET {', '.join(sets)} WHERE id = ?", params
            )

        for alias in aliases:
            alias = (alias or "").strip()
            if alias and alias.casefold() != name.casefold():
                self.conn.execute(
                    "INSERT OR IGNORE INTO entity_aliases(entity_id, alias) VALUES(?, ?)",
                    (entity_id, alias),
                )
        return entity_id

    def entity_names(self) -> dict[str, int]:
        """Every name and alias in play, mapped to its entity id (casefolded)."""
        out: dict[str, int] = {}
        for row in self.conn.execute("SELECT id, name FROM entities"):
            out[row["name"].casefold()] = int(row["id"])
        for row in self.conn.execute("SELECT entity_id, alias FROM entity_aliases"):
            out.setdefault(row["alias"].casefold(), int(row["entity_id"]))
        return out

    def mentioned_entities(self, text: str) -> list[int]:
        """Entity ids whose name or alias appears verbatim in `text`."""
        folded = (text or "").casefold()
        return [eid for name, eid in self.entity_names().items() if name and name in folded]

    # ---------------------------------------------------------------- facts

    def set_fact(self, entity_id: int, key: str, value: str, turn: int | None = None) -> None:
        """Record an attribute, superseding any current value for the same key."""
        turn = self.turn if turn is None else turn
        prior = self.conn.execute(
            "SELECT id, value FROM facts WHERE entity_id = ? AND key = ?"
            " AND superseded_by IS NULL",
            (entity_id, key),
        ).fetchone()
        if prior is not None and prior["value"] == value:
            return
        cur = self.conn.execute(
            "INSERT INTO facts(entity_id, key, value, turn) VALUES(?, ?, ?, ?)",
            (entity_id, key, value, turn),
        )
        if prior is not None:
            self.conn.execute(
                "UPDATE facts SET superseded_by = ? WHERE id = ?",
                (int(cur.lastrowid), int(prior["id"])),
            )

    def current_facts(self, entity_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM facts WHERE entity_id = ? AND superseded_by IS NULL"
                " ORDER BY key",
                (entity_id,),
            )
        )

    def fact_history(self, entity_id: int, key: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM facts WHERE entity_id = ? AND key = ? ORDER BY turn, id",
                (entity_id, key),
            )
        )

    # ------------------------------------------------------------ relations

    def set_relation(
        self,
        src_id: int,
        dst_id: int,
        kind: str,
        note: str = "",
        strength: int = 0,
        turn: int | None = None,
    ) -> None:
        turn = self.turn if turn is None else turn
        prior = self.conn.execute(
            "SELECT id FROM relations WHERE src_id = ? AND dst_id = ? AND kind = ?"
            " AND superseded_by IS NULL",
            (src_id, dst_id, kind),
        ).fetchone()
        cur = self.conn.execute(
            "INSERT INTO relations(src_id, dst_id, kind, note, strength, turn)"
            " VALUES(?, ?, ?, ?, ?, ?)",
            (src_id, dst_id, kind, note, _clamp(strength, -3, 3), turn),
        )
        if prior is not None:
            self.conn.execute(
                "UPDATE relations SET superseded_by = ? WHERE id = ?",
                (int(cur.lastrowid), int(prior["id"])),
            )

    def current_relations(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT r.*, s.name AS src_name, d.name AS dst_name FROM relations r"
                " JOIN entities s ON s.id = r.src_id JOIN entities d ON d.id = r.dst_id"
                " WHERE r.superseded_by IS NULL ORDER BY r.turn"
            )
        )

    # ---------------------------------------------------------------- flags

    def set_flag(self, key: str, value: str, turn: int | None = None) -> None:
        turn = self.turn if turn is None else turn
        self.conn.execute(
            "INSERT INTO flags(key, value, turn) VALUES(?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, turn = excluded.turn",
            (key, value, turn),
        )

    def flags(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM flags ORDER BY key"))

    # --------------------------------------------------------------- events

    def add_event(
        self,
        summary: str,
        turn: int | None = None,
        kind: str = "event",
        importance: int = 2,
        location: str = "",
        participants: Iterable[str] = (),
    ) -> int:
        summary = summary.strip()
        if not summary:
            raise ValueError("event summary must not be empty")
        turn = self.turn if turn is None else turn
        cur = self.conn.execute(
            "INSERT INTO events(turn, summary, kind, importance, location)"
            " VALUES(?, ?, ?, ?, ?)",
            (turn, summary, kind, _clamp(importance, 1, 5), location),
        )
        event_id = int(cur.lastrowid)

        names = [n.strip() for n in participants if n and n.strip()]
        for name in names:
            row = self.resolve_entity(name)
            entity_id = int(row["id"]) if row else self.upsert_entity(name, turn=turn)
            self.conn.execute(
                "INSERT OR IGNORE INTO event_participants(event_id, entity_id) VALUES(?, ?)",
                (event_id, entity_id),
            )

        # Index the summary together with participant and location names so that
        # "what happened with the innkeeper" matches events where the innkeeper
        # acted but was never named in the summary text.
        indexed = " ".join([summary, location, *names])
        self.conn.execute(
            "INSERT INTO events_fts(rowid, search_text) VALUES(?, ?)",
            (event_id, textutil.search_text(indexed)),
        )
        return event_id

    def event_participants(self, event_id: int) -> list[str]:
        return [
            r["name"]
            for r in self.conn.execute(
                "SELECT e.name FROM event_participants p JOIN entities e ON e.id = p.entity_id"
                " WHERE p.event_id = ? ORDER BY e.name",
                (event_id,),
            )
        ]

    # ------------------------------------------------------------ retrieval

    def retrieve(
        self,
        query: str,
        limit: int = 8,
        focus_entities: Sequence[int] = (),
        location: str = "",
        exclude_recent_turns: int = 0,
    ) -> list[RetrievedEvent]:
        """Rank episodic memories against the current moment.

        Four signals, because no single one is enough: lexical match finds the
        right topic, entity overlap finds "things involving this person" even
        when worded differently, importance keeps plot-critical beats reachable
        forever, and recency favours the immediate past when nothing else
        distinguishes two memories.
        """
        w = self.weights
        current_turn = self.turn
        cutoff = current_turn - exclude_recent_turns if exclude_recent_turns else None

        candidates: dict[int, sqlite3.Row] = {}
        lexical: dict[int, float] = {}

        match = textutil.fts_query(query)
        if match:
            rows = self.conn.execute(
                "SELECT e.*, bm25(events_fts) AS bm FROM events_fts"
                " JOIN events e ON e.id = events_fts.rowid"
                " WHERE events_fts MATCH ? AND e.consolidated_into IS NULL"
                " ORDER BY bm LIMIT ?",
                (match, max(limit * 8, 40)),
            ).fetchall()
            # bm25 is negative and unbounded; rank-normalise into 0..1 so the
            # weights below stay meaningful across queries of different lengths.
            for rank, row in enumerate(rows):
                candidates[int(row["id"])] = row
                lexical[int(row["id"])] = 1.0 - rank / max(len(rows), 1)

        focus = list(focus_entities)
        if focus:
            marks = ",".join("?" * len(focus))
            for row in self.conn.execute(
                f"SELECT DISTINCT e.* FROM events e"
                f" JOIN event_participants p ON p.event_id = e.id"
                f" WHERE p.entity_id IN ({marks}) AND e.consolidated_into IS NULL"
                f" ORDER BY e.importance DESC, e.turn DESC LIMIT ?",
                (*focus, max(limit * 8, 40)),
            ):
                candidates.setdefault(int(row["id"]), row)

        # Always let the highest-stakes memories compete, however the turn is worded.
        for row in self.conn.execute(
            "SELECT * FROM events WHERE consolidated_into IS NULL"
            " ORDER BY importance DESC, turn DESC LIMIT ?",
            (max(limit * 4, 20),),
        ):
            candidates.setdefault(int(row["id"]), row)

        focus_set = set(focus)
        scored: list[RetrievedEvent] = []
        for event_id, row in candidates.items():
            if cutoff is not None and int(row["turn"]) > cutoff:
                continue  # already in the verbatim window; don't pay for it twice

            participant_ids = {
                int(r["entity_id"])
                for r in self.conn.execute(
                    "SELECT entity_id FROM event_participants WHERE event_id = ?",
                    (event_id,),
                )
            }
            overlap = (
                len(focus_set & participant_ids) / len(focus_set) if focus_set else 0.0
            )
            age = max(current_turn - int(row["turn"]), 0)
            parts = {
                "lexical": w.lexical * lexical.get(event_id, 0.0),
                "entity": w.entity * overlap,
                "importance": w.importance * (int(row["importance"]) / 5.0),
                "recency": w.recency * math.exp(-age / max(w.recency_halflife, 1e-9)),
                "location": w.location
                * (1.0 if location and row["location"] == location else 0.0),
            }
            scored.append(
                RetrievedEvent(
                    id=event_id,
                    turn=int(row["turn"]),
                    summary=row["summary"],
                    importance=int(row["importance"]),
                    location=row["location"],
                    participants=self.event_participants(event_id),
                    score=sum(parts.values()),
                    parts=parts,
                )
            )

        scored.sort(key=lambda e: (-e.score, -e.turn))
        return scored[:limit]

    # ---------------------------------------------------------- consolidation

    def unconsolidated_before(self, turn: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM events WHERE consolidated_into IS NULL AND turn <= ?"
                " ORDER BY turn, id",
                (turn,),
            )
        )

    def add_chapter(
        self, title: str, summary: str, from_turn: int, to_turn: int, event_ids: Sequence[int]
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO chapters(title, summary, from_turn, to_turn, created_at)"
            " VALUES(?, ?, ?, ?, ?)",
            (title, summary, from_turn, to_turn, _now()),
        )
        chapter_id = int(cur.lastrowid)
        for event_id in event_ids:
            self.conn.execute(
                "UPDATE events SET consolidated_into = ? WHERE id = ?", (chapter_id, event_id)
            )
            # Consolidated events leave the retrieval pool but stay in the DB.
            self.conn.execute("DELETE FROM events_fts WHERE rowid = ?", (event_id,))
        return chapter_id

    def chapters(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM chapters ORDER BY from_turn"))

    # ----------------------------------------------------------- snapshots

    def state_snapshot(self, max_entities: int = 40) -> dict:
        """The always-in-context view of the world's current state."""
        entities = []
        rows = self.conn.execute(
            "SELECT * FROM entities ORDER BY importance DESC, last_turn DESC LIMIT ?",
            (max_entities,),
        ).fetchall()
        for row in rows:
            entities.append(
                {
                    "id": int(row["id"]),
                    "name": row["name"],
                    "kind": row["kind"],
                    "status": row["status"],
                    "summary": row["summary"],
                    "importance": int(row["importance"]),
                    "last_turn": int(row["last_turn"]),
                    "facts": {f["key"]: f["value"] for f in self.current_facts(int(row["id"]))},
                }
            )
        return {
            "turn": self.turn,
            "entities": entities,
            "relations": [
                {
                    "src": r["src_name"],
                    "kind": r["kind"],
                    "dst": r["dst_name"],
                    "strength": int(r["strength"]),
                    "note": r["note"],
                }
                for r in self.current_relations()
            ],
            "flags": {f["key"]: f["value"] for f in self.flags()},
        }

    def stats(self) -> dict:
        def count(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "turns": self.turn,
            "entities": count("SELECT COUNT(*) FROM entities"),
            "facts": count("SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL"),
            "relations": count("SELECT COUNT(*) FROM relations WHERE superseded_by IS NULL"),
            "flags": count("SELECT COUNT(*) FROM flags"),
            "events": count("SELECT COUNT(*) FROM events"),
            "events_live": count("SELECT COUNT(*) FROM events WHERE consolidated_into IS NULL"),
            "chapters": count("SELECT COUNT(*) FROM chapters"),
        }
