"""The game loop: one player action in, one turn of story out.

Each turn is four steps:

1. **Recall**  — score episodic memory against what the player just did.
2. **Narrate** — stream prose constrained by state, chapters and recall.
3. **Record**  — the archivist reads that prose back into structured memory.
4. **Consolidate** — occasionally, compress old memory so context stays bounded.

Step 3 failing must never cost the player their turn: the prose is committed
first, and a memory-write failure is reported rather than raised.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import archivist, context, db, narrator
from .config import EngineConfig
from .memory import MemoryStore, RetrievedEvent
from .world import World

OPENING_INPUT = "（故事開始）"


@dataclass
class TurnResult:
    turn: int
    narration: str
    memories: list[RetrievedEvent] = field(default_factory=list)
    written: dict[str, int] = field(default_factory=dict)
    consolidated: str = ""
    warning: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class Engine:
    def __init__(
        self,
        conn: sqlite3.Connection,
        world: World,
        client=None,
        config: EngineConfig | None = None,
    ):
        self.conn = conn
        self.world = world
        self.client = client
        self.config = config or EngineConfig()
        self.store = MemoryStore(conn, self.config.weights)

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def create(
        cls,
        path: str | Path,
        world: World,
        client=None,
        config: EngineConfig | None = None,
    ) -> "Engine":
        """Start a new story.  Seeding is offline — no API call required."""
        conn = db.connect(path)
        if db.get_meta(conn, "world") is not None:
            raise FileExistsError(f"{path} 已經是一個存檔了")
        db.set_meta(conn, "world", world.to_dict())
        engine = cls(conn, world, client, config)
        engine._seed()
        conn.commit()
        return engine

    @classmethod
    def load(
        cls, path: str | Path, client=None, config: EngineConfig | None = None
    ) -> "Engine":
        conn = db.connect(path)
        data = db.get_meta(conn, "world")
        if data is None:
            raise ValueError(f"{path} 不是一個 qooq 存檔（缺少 world）")
        return cls(conn, World.from_dict(data), client, config)

    def _seed(self) -> None:
        """Load the authored world into memory as turn 0."""
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

    # ----------------------------------------------------------------- play

    def recall(self, player_input: str) -> list[RetrievedEvent]:
        """What should the narrator be reminded of, given this action?"""
        recent = self.store.recent_turns(1)
        # Include the last narration in the query: the player's action is often
        # short ("問他那件事"), and the scene supplies the missing nouns.
        query = player_input
        if recent:
            query = f"{player_input}\n{recent[-1]['narration']}"

        focus = self.store.mentioned_entities(query)
        scene = self.store.conn.execute(
            "SELECT value FROM flags WHERE key = '__scene__'"
        ).fetchone()
        return self.store.retrieve(
            query=query,
            limit=self.config.retrieved_memories,
            focus_entities=focus,
            location=scene["value"] if scene else "",
            exclude_recent_turns=self.config.verbatim_turns,
        )

    def play(self, player_input: str, on_text=None) -> TurnResult:
        if self.client is None:
            raise RuntimeError("engine has no client; cannot generate narration")

        player_input = player_input.strip()
        if not player_input:
            raise ValueError("行動不可為空")

        memories = self.recall(player_input)
        system, messages = context.build_turn_request(
            self.store, self.world, self.config, player_input, memories
        )
        narration, usage = narrator.narrate(
            self.client, system, messages, self.config, on_text
        )

        turn = self.store.turn + 1
        self.store.record_turn(
            turn,
            player_input,
            narration,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
        self.conn.commit()  # the prose is safe before memory work begins

        result = TurnResult(
            turn=turn,
            narration=narration,
            memories=memories,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
        )

        try:
            delta = archivist.extract(
                self.client, self.store, self.world, self.config, player_input, narration
            )
            if delta is None:
                result.warning = "記錄員沒有回傳可用的紀錄，這一回合未寫入長期記憶。"
            else:
                result.written = archivist.apply_delta(self.store, delta, turn)
            self.conn.commit()
        except Exception as exc:  # keep playing; the turn's prose is already saved
            self.conn.rollback()
            result.warning = f"寫入長期記憶失敗：{exc}"

        try:
            result.consolidated = self.maybe_consolidate()
        except Exception as exc:
            result.warning = (result.warning + " ").strip() + f"章節壓縮失敗：{exc}"

        return result

    # -------------------------------------------------------- consolidation

    def maybe_consolidate(self) -> str:
        """Compress old episodic memory once it stops paying for itself."""
        live = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE consolidated_into IS NULL"
            ).fetchone()[0]
        )
        if live < self.config.consolidate_after_events:
            return ""

        cutoff = self.store.turn - self.config.consolidate_keep_turns
        events = self.store.unconsolidated_before(cutoff)
        if len(events) < 5:
            return ""

        from_turn, to_turn = int(events[0]["turn"]), int(events[-1]["turn"])
        title, summary = narrator.write_chapter(
            self.client, self.config, events, from_turn, to_turn
        )
        self.store.add_chapter(
            title, summary, from_turn, to_turn, [int(e["id"]) for e in events]
        )
        self.conn.commit()
        return f"{title}（第{from_turn}–{to_turn}回合，壓縮 {len(events)} 則記憶）"

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
