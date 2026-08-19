"""The game loop, as a two-step cycle around a human courier.

There is no API call anywhere in this system.  A turn is:

    begin_turn(action) -> prompt      the player copies this into their AI
    ingest(reply)      -> TurnResult  the player pastes the AI's reply back

Between those two calls the turn is *pending*, and that state lives in the
database rather than in memory — the player may close the browser, paste into
their AI, and come back an hour later.

Everything that decides what the AI is told (recall, briefing assembly) and
everything that reads its reply back into structured memory happens here, in
plain Python.  The AI only ever writes prose and one JSON block.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import context, db, protocol
from .config import EngineConfig
from .memory import MemoryStore, RetrievedEvent
from .protocol import Record
from .world import World

OPENING_INPUT = "（故事開始）"
_PENDING_KEY = "pending_action"

EVENT_KINDS = (
    "meeting", "discovery", "conflict", "promise", "loss",
    "travel", "revelation", "change", "event",
)


@dataclass
class PendingTurn:
    action: str
    prompt: str
    memories: list[RetrievedEvent] = field(default_factory=list)


@dataclass
class TurnResult:
    turn: int
    narration: str
    written: dict[str, int] = field(default_factory=dict)
    warning: str = ""


class Engine:
    def __init__(
        self, conn: sqlite3.Connection, world: World, config: EngineConfig | None = None
    ):
        self.conn = conn
        self.world = world
        self.config = config or EngineConfig()
        self.store = MemoryStore(conn, self.config.weights)

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls, path: str | Path, world: World, config: EngineConfig | None = None
    ) -> "Engine":
        """Start a new story.  Entirely offline — seeds go straight to disk."""
        conn = db.connect(path)
        if db.get_meta(conn, "world") is not None:
            raise FileExistsError(f"{path} 已經是一個存檔了")
        db.set_meta(conn, "world", world.to_dict())
        engine = cls(conn, world, config)
        engine._seed()
        conn.commit()
        return engine

    @classmethod
    def load(
        cls,
        path: str | Path,
        config: EngineConfig | None = None,
        same_thread: bool = True,
    ) -> "Engine":
        conn = db.connect(path, same_thread=same_thread)
        data = db.get_meta(conn, "world")
        if data is None:
            raise ValueError(f"{path} 不是一個 qooq 存檔（缺少 world）")
        return cls(conn, World.from_dict(data), config)

    def _seed(self) -> None:
        for seed in self.world.seed_entities:
            entity_id = self.store.upsert_entity(
                name=seed.name,
                kind=seed.kind,
                summary=seed.summary,
                importance=seed.importance,
                aliases=seed.aliases,
                turn=0,
            )
            for key, value in seed.facts.items():
                self.store.set_fact(entity_id, key, value, 0)

        for key, value in self.world.seed_flags.items():
            self.store.set_flag(key, value, 0)

        opening = (self.world.opening or self.world.premise).strip()
        self.store.record_turn(0, OPENING_INPUT, opening)
        self.store.add_event(
            summary=f"故事開始：{opening.splitlines()[0][:80]}",
            turn=0,
            kind="event",
            importance=3,
            participants=[s.name for s in self.world.seed_entities if s.kind == "character"][:4],
        )

    # ----------------------------------------------------------- the cycle

    def recall(self, player_input: str) -> list[RetrievedEvent]:
        """Score episodic memory against what the player just did."""
        recent = self.store.recent_turns(1)
        # The player's action is often too short to retrieve on ("問他那件事"),
        # so the previous narration supplies the missing nouns.
        query = player_input
        if recent:
            query = f"{player_input}\n{recent[-1]['narration']}"

        focus = self.store.mentioned_entities(query)
        scene = self.conn.execute(
            "SELECT value FROM flags WHERE key = '__scene__'"
        ).fetchone()
        return self.store.retrieve(
            query=query,
            limit=self.config.retrieved_memories,
            focus_entities=focus,
            location=scene["value"] if scene else "",
            exclude_recent_turns=self.config.verbatim_turns,
        )

    def begin_turn(self, action: str) -> PendingTurn:
        """Build the prompt for one action and mark the turn pending."""
        action = action.strip()
        if not action:
            raise ValueError("行動不可為空")

        memories = self.recall(action)
        prompt = protocol.build_prompt(
            craft=context.NARRATOR_CRAFT,
            world=self.world.render(),
            briefing=context.build_briefing(self.store, self.config, memories),
            recent=[
                (row["player_input"], row["narration"])
                for row in self.store.recent_turns(self.config.verbatim_turns)
            ],
            action=action,
            language=self.world.language,
        )
        db.set_meta(self.conn, _PENDING_KEY, action)
        self.conn.commit()
        return PendingTurn(action=action, prompt=prompt, memories=memories)

    def pending_action(self) -> str | None:
        return db.get_meta(self.conn, _PENDING_KEY)

    def cancel_pending(self) -> None:
        self.conn.execute("DELETE FROM meta WHERE key = ?", (_PENDING_KEY,))
        self.conn.commit()

    def ingest(self, reply: str, action: str | None = None) -> TurnResult:
        """Read the AI's reply back into the story.

        The prose is committed before the memory record is applied, so a
        malformed JSON block costs a memory write, never the player's turn.
        """
        action = action or self.pending_action()
        if not action:
            raise ValueError("沒有進行中的回合，請先送出一個行動")

        parsed = protocol.parse_response(reply)
        if not parsed.prose.strip():
            raise ValueError("貼上的內容沒有可用的敘述文字")

        turn = self.store.turn + 1
        self.store.record_turn(turn, action, parsed.prose)
        self.cancel_pending()
        self.conn.commit()

        result = TurnResult(turn=turn, narration=parsed.prose, warning=parsed.warning)
        if parsed.record is None:
            return result

        try:
            result.written = self.apply_record(parsed.record, turn)
            self.conn.commit()
        except Exception as exc:
            self.conn.rollback()
            result.warning = (result.warning + " ").strip() + f"寫入長期記憶失敗：{exc}"
        return result

    # ------------------------------------------------------- memory writing

    def apply_record(self, record: Record, turn: int) -> dict[str, int]:
        """Fold a parsed record into the store, skipping anything unusable.

        Written defensively: the record comes from free-form model output, so
        every field is treated as possibly missing, mistyped, or empty.
        """
        counts = {"entities": 0, "facts": 0, "events": 0, "relations": 0, "flags": 0}

        def text(value) -> str:
            return str(value).strip() if value is not None else ""

        def score(value, default: int = 2) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        for entity in record.entities:
            name = text(entity.get("name"))
            if not name:
                continue
            entity_id = self.store.upsert_entity(
                name=name,
                kind=text(entity.get("kind")) or "concept",
                summary=text(entity.get("summary")),
                status=text(entity.get("status")),
                importance=score(entity.get("importance"), 2)
                if entity.get("importance") is not None
                else None,
                aliases=entity.get("aliases") or [],
                turn=turn,
            )
            counts["entities"] += 1
            for key, value in (entity.get("facts") or {}).items():
                if text(key) and text(value):
                    self.store.set_fact(entity_id, text(key), text(value), turn)
                    counts["facts"] += 1

        for event in record.events:
            summary = text(event.get("summary"))
            if not summary:
                continue
            kind = text(event.get("kind"))
            self.store.add_event(
                summary=summary,
                turn=turn,
                kind=kind if kind in EVENT_KINDS else "event",
                importance=score(event.get("importance"), 2),
                location=text(event.get("location")) or record.scene,
                participants=[
                    text(p) for p in (event.get("participants") or []) if text(p)
                ],
            )
            counts["events"] += 1

        for relation in record.relations:
            src = text(relation.get("from") or relation.get("source"))
            dst = text(relation.get("to") or relation.get("target"))
            if not (src and dst):
                continue
            self.store.set_relation(
                self.store.upsert_entity(src, turn=turn),
                self.store.upsert_entity(dst, turn=turn),
                text(relation.get("kind")) or "關聯",
                text(relation.get("note")),
                score(relation.get("strength"), 0),
                turn,
            )
            counts["relations"] += 1

        for key, value in record.flags.items():
            if text(key):
                self.store.set_flag(text(key), text(value), turn)
                counts["flags"] += 1

        if record.scene:
            self.store.set_flag("__scene__", record.scene, turn)

        return counts

    # -------------------------------------------------------- consolidation

    def consolidation_due(self) -> bool:
        live = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE consolidated_into IS NULL"
            ).fetchone()[0]
        )
        if live < self.config.consolidate_after_events:
            return False
        cutoff = self.store.turn - self.config.consolidate_keep_turns
        return len(self.store.unconsolidated_before(cutoff)) >= 5

    def consolidation_prompt(self) -> str:
        """A second, rarer paste: compress old events into a chapter summary."""
        cutoff = self.store.turn - self.config.consolidate_keep_turns
        events = self.store.unconsolidated_before(cutoff)
        if len(events) < 5:
            raise ValueError("目前沒有足夠的舊記憶需要壓縮")
        listing = "\n".join(
            f"- [第{e['turn']}回合｜重要度{e['importance']}] {e['summary']}" for e in events
        )
        return protocol.build_chapter_prompt(
            listing, int(events[0]["turn"]), int(events[-1]["turn"])
        )

    def ingest_chapter(self, reply: str) -> str:
        """Store a pasted chapter summary and retire the events it covers."""
        cutoff = self.store.turn - self.config.consolidate_keep_turns
        events = self.store.unconsolidated_before(cutoff)
        if len(events) < 5:
            raise ValueError("目前沒有足夠的舊記憶需要壓縮")

        title, summary = protocol.parse_chapter(reply)
        from_turn, to_turn = int(events[0]["turn"]), int(events[-1]["turn"])
        self.store.add_chapter(
            title, summary, from_turn, to_turn, [int(e["id"]) for e in events]
        )
        self.conn.commit()
        return f"{title}（第{from_turn}–{to_turn}回合，壓縮 {len(events)} 則記憶）"

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
