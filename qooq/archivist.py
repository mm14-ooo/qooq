"""The archivist: turns each turn's prose into structured long-term memory.

This is the half of the system that makes memory *durable*.  The narrator
writes fiction; the archivist reads that fiction back and decides what the
story must never forget — who appeared, what changed, what was promised.

All schema fields are required (no optionals): strict JSON-schema output is
only worth having if the shape is guaranteed, and "unknown" is expressed as an
empty string rather than an absent key.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .config import EngineConfig
from .memory import MemoryStore
from .world import World

EVENT_KINDS = (
    "meeting", "discovery", "conflict", "promise", "loss",
    "travel", "revelation", "change", "event",
)


class FactRecord(BaseModel):
    key: str = Field(description="屬性名稱，例如「職業」「持有物」「傷勢」「動機」")
    value: str = Field(description="屬性的當前值")


class EntityRecord(BaseModel):
    name: str = Field(description="正式名稱，與既有紀錄中的寫法完全一致")
    kind: str = Field(description="character / location / item / faction / concept 之一")
    summary: str = Field(description="一句話描述；若無新資訊則留空字串")
    status: str = Field(description="active / absent / dead / destroyed / unknown；不變則留空字串")
    importance: int = Field(description="1（背景）到 5（故事核心）")
    aliases: list[str] = Field(description="別名、綽號、代稱；沒有則空陣列")
    facts: list[FactRecord] = Field(description="這一回合確立或改變的屬性")


class EventRecord(BaseModel):
    summary: str = Field(description="一句話陳述發生了什麼，寫入具體名詞而非代名詞")
    kind: str = Field(description=f"下列之一：{', '.join(EVENT_KINDS)}")
    importance: int = Field(
        description="1=氛圍細節，2=一般互動，3=有後果的行動，4=劇情轉折，5=不可逆的重大事件"
    )
    location: str = Field(description="發生地點名稱；不明則留空字串")
    participants: list[str] = Field(description="涉及的角色或事物名稱")


class RelationRecord(BaseModel):
    source: str
    target: str
    kind: str = Field(description="關係類型，例如「信任」「虧欠」「敵對」「兄妹」")
    strength: int = Field(description="-3（死敵）到 +3（生死之交）")
    note: str = Field(description="簡短說明；不需要則留空字串")


class FlagRecord(BaseModel):
    key: str = Field(description="劇情狀態名稱，例如「地窖門」「委託進度」")
    value: str = Field(description="狀態值，例如「已開啟」「第二階段」")


class MemoryDelta(BaseModel):
    """Everything the story learned from one turn."""

    scene_location: str = Field(description="這一回合結束時主角所在的地點；不明則留空字串")
    events: list[EventRecord]
    entities: list[EntityRecord]
    relations: list[RelationRecord]
    flags: list[FlagRecord]


ARCHIVIST_SYSTEM = """\
你是一部互動小說的記錄員。你會讀到剛剛發生的一個回合，把其中「這個故事日後必須記得的事」
轉成結構化紀錄。你不寫小說，只做紀錄。

原則：
- 只記錄這一回合新出現或改變的資訊。已經在既有紀錄中且未改變的，不要重複輸出。
- 事件摘要要寫成獨立可讀的句子：用具體名詞取代「他」「那個東西」，因為日後讀到它時沒有上下文。
- 重要度要誠實。氛圍描寫是 1，承諾、背叛、死亡、獲得關鍵物品是 4–5。
  全部標成高分等於沒有排序。
- 名稱必須沿用既有紀錄的寫法，不要創造同一角色的第二種稱呼；別名放進 aliases。
- 只記錄敘述中確實發生的事，不要推測後續發展。
- 沒有內容的欄位就給空陣列或空字串，不要編造。
"""


def _delta_to_dict(delta: MemoryDelta) -> dict[str, Any]:
    return delta.model_dump()


def extract(
    client,
    store: MemoryStore,
    world: World,
    config: EngineConfig,
    player_input: str,
    narration: str,
) -> MemoryDelta | None:
    """Ask the archivist what this turn added to the story.

    Returns None if the model could not produce a usable record; the caller
    keeps playing rather than losing the turn.
    """
    state = store.state_snapshot(config.max_state_entities)
    known = ", ".join(e["name"] for e in state["entities"]) or "（尚無）"

    prompt = (
        f"# 作品\n{world.title}\n\n"
        f"# 既有紀錄中的名稱（請沿用這些寫法）\n{known}\n\n"
        f"# 目前旗標\n"
        + ("\n".join(f"- {k} = {v}" for k, v in state["flags"].items()) or "（尚無）")
        + f"\n\n# 第 {store.turn + 1} 回合\n\n【玩家行動】\n{player_input}\n\n"
        f"【敘述】\n{narration}\n\n"
        "請輸出這一回合的紀錄。"
    )

    response = client.messages.parse(
        model=config.archivist_model,
        max_tokens=config.archivist_max_tokens,
        system=ARCHIVIST_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=MemoryDelta,
    )
    return getattr(response, "parsed_output", None)


def apply_delta(store: MemoryStore, delta: MemoryDelta, turn: int) -> dict[str, int]:
    """Write a delta into the store.  Idempotent per (entity, key) by design."""
    counts = {"events": 0, "entities": 0, "facts": 0, "relations": 0, "flags": 0}

    for entity in delta.entities:
        if not entity.name.strip():
            continue
        entity_id = store.upsert_entity(
            name=entity.name,
            kind=entity.kind,
            summary=entity.summary,
            status=entity.status,
            importance=entity.importance,
            aliases=entity.aliases,
            turn=turn,
        )
        counts["entities"] += 1
        for fact in entity.facts:
            if fact.key.strip() and fact.value.strip():
                store.set_fact(entity_id, fact.key.strip(), fact.value.strip(), turn)
                counts["facts"] += 1

    for event in delta.events:
        if not event.summary.strip():
            continue
        store.add_event(
            summary=event.summary,
            turn=turn,
            kind=event.kind if event.kind in EVENT_KINDS else "event",
            importance=event.importance,
            location=event.location or delta.scene_location,
            participants=event.participants,
        )
        counts["events"] += 1

    for relation in delta.relations:
        if not (relation.source.strip() and relation.target.strip()):
            continue
        src = store.upsert_entity(relation.source, turn=turn)
        dst = store.upsert_entity(relation.target, turn=turn)
        store.set_relation(src, dst, relation.kind, relation.note, relation.strength, turn)
        counts["relations"] += 1

    for flag in delta.flags:
        if flag.key.strip():
            store.set_flag(flag.key.strip(), flag.value, turn)
            counts["flags"] += 1

    if delta.scene_location.strip():
        store.set_flag("__scene__", delta.scene_location.strip(), turn)

    return counts
