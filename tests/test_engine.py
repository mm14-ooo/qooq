import pytest
from fake_client import FakeClient, delta

from qooq import context, db
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


def make_engine(tmp_path, client=None, config=None):
    return Engine.create(tmp_path / "s.db", WORLD, client=client, config=config)


def test_new_save_seeds_offline(tmp_path):
    engine = make_engine(tmp_path)
    assert engine.store.turn == 0
    assert engine.store.recent_turns(1)[0]["narration"] == "雨沒有停。"
    assert engine.store.state_snapshot()["flags"]["夜次"] == "第七夜"


def test_cannot_create_over_an_existing_save(tmp_path):
    make_engine(tmp_path).close()
    with pytest.raises(FileExistsError):
        Engine.create(tmp_path / "s.db", WORLD)


def test_load_restores_the_world_bible(tmp_path):
    make_engine(tmp_path).close()
    engine = Engine.load(tmp_path / "s.db")
    assert engine.world.title == "測試作品"
    assert engine.world.rules == ["讀取記憶必然有代價。"]


def test_play_writes_prose_and_memory(tmp_path):
    client = FakeClient(
        narrations=["老陳推門進來，肩上濕透。"],
        deltas=[
            delta(
                location="當鋪",
                events=[{"summary": "老陳冒雨走進當鋪", "importance": 3,
                         "participants": ["老陳", "掌櫃"]}],
                entities=[{"name": "老陳", "kind": "character", "summary": "碼頭工人",
                           "facts": [{"key": "狀態", "value": "全身濕透"}]}],
                relations=[{"source": "掌櫃", "target": "老陳", "kind": "認識",
                            "strength": 1}],
                flags=[{"key": "門外", "value": "已進門"}],
            )
        ],
    )
    engine = make_engine(tmp_path, client)
    chunks = []
    result = engine.play("開門", on_text=chunks.append)

    assert result.turn == 1
    assert result.narration == "老陳推門進來，肩上濕透。"
    assert "".join(chunks) == result.narration  # streamed incrementally
    assert not result.warning

    snap = engine.store.state_snapshot()
    names = {e["name"] for e in snap["entities"]}
    assert {"老陳", "掌櫃"} <= names
    assert snap["flags"]["門外"] == "已進門"
    assert snap["flags"]["__scene__"] == "當鋪"
    chen = engine.store.resolve_entity("老陳")
    assert {f["key"]: f["value"] for f in engine.store.current_facts(chen["id"])} == {
        "狀態": "全身濕透"
    }


def test_prose_survives_an_archivist_failure(tmp_path):
    class Broken(FakeClient):
        def next_delta(self):
            raise RuntimeError("schema boom")

    engine = make_engine(tmp_path, Broken(narrations=["門開了。"]))
    result = engine.play("開門")

    assert result.narration == "門開了。"
    assert "寫入長期記憶失敗" in result.warning
    # The turn is still on disk after the rollback of the memory write.
    reloaded = Engine.load(tmp_path / "s.db")
    assert reloaded.store.turn == 1
    assert reloaded.store.recent_turns(1)[0]["narration"] == "門開了。"


def test_unparseable_extraction_is_reported_not_raised(tmp_path):
    client = FakeClient(narrations=["門開了。"], deltas=[None])
    result = make_engine(tmp_path, client).play("開門")
    assert "未寫入長期記憶" in result.warning


def test_empty_action_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        make_engine(tmp_path, FakeClient()).play("   ")


def test_recall_feeds_retrieved_memories_into_the_prompt(tmp_path):
    client = FakeClient()
    engine = make_engine(tmp_path, client)
    engine.store.add_event("老陳把一枚銅鑰匙押在當鋪", turn=1, importance=4,
                           participants=["老陳"])
    for n in range(2, 12):
        engine.store.record_turn(n, f"行動{n}", f"敘述{n}")
    engine.conn.commit()

    engine.play("我想起那枚銅鑰匙")

    briefing = client.stream_calls[0]["messages"][-1]["content"]
    assert "銅鑰匙" in briefing


def test_consolidation_compresses_old_memory(tmp_path):
    config = EngineConfig()
    config.consolidate_after_events = 8
    config.consolidate_keep_turns = 2

    client = FakeClient()
    engine = make_engine(tmp_path, client, config)
    for n in range(1, 15):
        engine.store.record_turn(n, f"行動{n}", f"敘述{n}")
        engine.store.add_event(f"事件{n}", turn=n, importance=2)
    engine.conn.commit()

    client.narrations = ["第十五回合的敘述。", "序章\n\n掌櫃度過了漫長的七夜。"]
    result = engine.play("繼續")

    assert result.consolidated
    chapters = engine.store.chapters()
    assert len(chapters) == 1
    assert chapters[0]["title"] == "序章"
    assert "漫長的七夜" in chapters[0]["summary"]
    # Compressed events stay in the database but leave the retrieval pool.
    stats = engine.store.stats()
    assert stats["events"] > stats["events_live"]


def test_request_shape_matches_the_api_contract(tmp_path):
    client = FakeClient(narrations=["好。"])
    engine = make_engine(tmp_path, client)
    engine.play("開門")

    call = client.stream_calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["effort"] == "high"
    # Opus 5 removed sampling parameters and assistant prefill.
    assert "temperature" not in call and "top_p" not in call
    assert call["messages"][-1]["role"] != "assistant"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}

    parse = client.parse_calls[0]
    assert parse["output_format"].__name__ == "MemoryDelta"


def test_memory_briefing_uses_the_operator_channel_on_supporting_models(tmp_path):
    client = FakeClient(narrations=["好。"])
    engine = make_engine(tmp_path, client)
    engine.play("開門")

    messages = client.stream_calls[0]["messages"]
    assert messages[-1]["role"] == "system"          # briefing = operator data
    assert messages[-2]["role"] == "user"            # action = player data
    assert "【玩家行動】開門" in messages[-2]["content"]


def test_briefing_falls_back_inline_on_models_without_system_messages(tmp_path):
    config = EngineConfig()
    config.narrator_model = "claude-sonnet-5"
    client = FakeClient(narrations=["好。"])
    engine = make_engine(tmp_path, client, config)
    engine.play("開門")

    messages = client.stream_calls[0]["messages"]
    assert all(m["role"] != "system" for m in messages)
    assert "【玩家行動】開門" in messages[-1]["content"]


def test_supports_system_messages_gate():
    assert context.supports_system_messages("claude-opus-5")
    assert context.supports_system_messages("claude-fable-5")
    assert not context.supports_system_messages("claude-sonnet-5")
    assert not context.supports_system_messages("claude-haiku-4-5")
