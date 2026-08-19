import json

import pytest

from qooq.config import EngineConfig
from qooq.engine import Engine
from qooq.world import World

WORLD = World(
    title="測試作品",
    premise="一間當鋪。",
    opening="雨沒有停。",
    rules=["讀取記憶必然有代價。"],
    seed_flags={"夜次": "第七夜"},
)


def reply(prose="老陳推門進來，肩上濕透。", **record):
    payload = {"scene": "", "events": [], "entities": [], "relations": [], "flags": {}}
    payload.update(record)
    return f"{prose}\n\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```"


@pytest.fixture
def engine(tmp_path):
    return Engine.create(tmp_path / "s.db", WORLD)


def test_new_save_seeds_offline(engine):
    assert engine.store.turn == 0
    assert engine.store.recent_turns(1)[0]["narration"] == "雨沒有停。"
    assert engine.store.state_snapshot()["flags"]["夜次"] == "第七夜"


def test_cannot_create_over_an_existing_save(tmp_path):
    Engine.create(tmp_path / "s.db", WORLD).close()
    with pytest.raises(FileExistsError):
        Engine.create(tmp_path / "s.db", WORLD)


def test_load_restores_the_world_bible(tmp_path):
    Engine.create(tmp_path / "s.db", WORLD).close()
    reloaded = Engine.load(tmp_path / "s.db")
    assert reloaded.world.title == "測試作品"
    assert reloaded.world.rules == ["讀取記憶必然有代價。"]


def test_prompt_carries_world_state_and_action(engine):
    pending = engine.begin_turn("開門")
    assert "測試作品" in pending.prompt
    assert "讀取記憶必然有代價。" in pending.prompt
    assert "第七夜" in pending.prompt          # seeded flag
    assert "雨沒有停。" in pending.prompt        # verbatim opening
    assert "開門" in pending.prompt


def test_pending_state_survives_a_reload(tmp_path):
    engine = Engine.create(tmp_path / "s.db", WORLD)
    engine.begin_turn("開門")
    engine.close()

    # The player closed the browser, pasted into their AI, and came back.
    reopened = Engine.load(tmp_path / "s.db")
    assert reopened.pending_action() == "開門"
    result = reopened.ingest(reply())
    assert result.turn == 1
    assert reopened.pending_action() is None


def test_ingest_writes_prose_and_memory(engine):
    engine.begin_turn("開門")
    result = engine.ingest(reply(
        scene="當鋪",
        events=[{"summary": "老陳冒雨走進當鋪", "importance": 3,
                 "participants": ["老陳", "掌櫃"]}],
        entities=[{"name": "老陳", "kind": "character", "summary": "碼頭工人",
                   "facts": {"狀態": "全身濕透"}}],
        relations=[{"from": "掌櫃", "to": "老陳", "kind": "認識", "strength": 1}],
        flags={"門外": "已進門"},
    ))

    assert result.turn == 1
    assert not result.warning
    # entities counts what the record declared; 掌櫃 is auto-created by the
    # relation rather than declared, so it is not counted here.
    assert result.written == {"entities": 1, "facts": 1, "events": 1,
                              "relations": 1, "flags": 1}

    snap = engine.store.state_snapshot()
    assert {"老陳", "掌櫃"} <= {e["name"] for e in snap["entities"]}
    assert snap["flags"]["門外"] == "已進門"
    assert snap["flags"]["__scene__"] == "當鋪"
    chen = engine.store.resolve_entity("老陳")
    assert {f["key"]: f["value"] for f in engine.store.current_facts(chen["id"])} == {
        "狀態": "全身濕透"
    }


def test_prose_is_kept_when_the_json_is_missing(engine):
    engine.begin_turn("開門")
    result = engine.ingest("門開了，什麼也沒有。")
    assert result.turn == 1
    assert result.narration == "門開了，什麼也沒有。"
    assert "沒有寫入長期記憶" in result.warning
    assert engine.store.turn == 1


def test_ingest_without_a_pending_turn_is_rejected(engine):
    with pytest.raises(ValueError):
        engine.ingest(reply())


def test_ingest_with_an_explicit_action_bypasses_pending(engine):
    assert engine.ingest(reply(), action="臨時補一回合").turn == 1


def test_empty_action_is_rejected(engine):
    with pytest.raises(ValueError):
        engine.begin_turn("   ")


def test_empty_prose_is_rejected(engine):
    engine.begin_turn("開門")
    with pytest.raises(ValueError):
        engine.ingest("```json\n{}\n```")


def test_cancel_clears_the_pending_turn(engine):
    engine.begin_turn("開門")
    engine.cancel_pending()
    assert engine.pending_action() is None


def test_recall_feeds_relevant_memories_into_the_prompt(engine):
    engine.store.add_event("老陳把一枚銅鑰匙押在當鋪", turn=1, importance=4,
                           participants=["老陳"])
    for n in range(2, 12):
        engine.store.record_turn(n, f"行動{n}", f"敘述{n}")
    engine.conn.commit()

    prompt = engine.begin_turn("我想起那枚銅鑰匙").prompt
    assert "銅鑰匙" in prompt


def test_malformed_importance_does_not_break_the_write(engine):
    engine.begin_turn("開門")
    result = engine.ingest(reply(
        events=[{"summary": "有人進來了", "importance": "很高"}],
        entities=[{"name": "訪客", "importance": None}],
    ))
    assert result.written["events"] == 1
    assert engine.store.conn.execute(
        "SELECT importance FROM events WHERE summary = '有人進來了'"
    ).fetchone()[0] == 2   # fell back to the default rather than raising


def test_nameless_records_are_skipped_not_fatal(engine):
    engine.begin_turn("開門")
    result = engine.ingest(reply(
        entities=[{"name": "  "}, {"name": "老陳"}],
        events=[{"summary": ""}, {"summary": "老陳進來了"}],
        relations=[{"from": "老陳", "to": ""}],
    ))
    assert result.written == {"entities": 1, "facts": 0, "events": 1,
                              "relations": 0, "flags": 0}


def test_consolidation_becomes_due_and_compresses(tmp_path):
    config = EngineConfig()
    config.consolidate_after_events = 8
    config.consolidate_keep_turns = 2

    engine = Engine.create(tmp_path / "s.db", WORLD, config)
    assert not engine.consolidation_due()

    for n in range(1, 15):
        engine.store.record_turn(n, f"行動{n}", f"敘述{n}")
        engine.store.add_event(f"事件{n}", turn=n, importance=2)
    engine.conn.commit()

    assert engine.consolidation_due()
    prompt = engine.consolidation_prompt()
    assert "事件1" in prompt and "輸出格式" in prompt

    message = engine.ingest_chapter("序章\n\n掌櫃度過了漫長的七夜。")
    assert "序章" in message
    chapters = engine.store.chapters()
    assert len(chapters) == 1 and "漫長的七夜" in chapters[0]["summary"]

    stats = engine.store.stats()
    assert stats["events"] > stats["events_live"]   # kept on disk, out of recall
    assert not engine.consolidation_due()
