import sqlite3

import pytest

from qooq import db
from qooq.memory import MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(db.connect(tmp_path / "s.db"))


def test_entity_resolution_by_alias_and_case(store):
    eid = store.upsert_entity("老陳", kind="character", aliases=["陳掌櫃", "the old man"])
    assert store.resolve_entity("老陳")["id"] == eid
    assert store.resolve_entity("陳掌櫃")["id"] == eid
    assert store.resolve_entity("THE OLD MAN")["id"] == eid
    assert store.resolve_entity("不存在的人") is None


def test_upsert_only_overwrites_supplied_fields(store):
    eid = store.upsert_entity("老陳", kind="character", summary="當鋪常客", importance=4)
    store.upsert_entity("老陳", summary="", importance=None)  # a bare mention
    row = store.resolve_entity("老陳")
    assert row["summary"] == "當鋪常客"
    assert row["importance"] == 4
    assert row["kind"] == "character"


def test_facts_supersede_rather_than_overwrite(store):
    eid = store.upsert_entity("老陳")
    store.set_fact(eid, "傷勢", "無", turn=1)
    store.set_fact(eid, "傷勢", "左肩中刀", turn=5)

    current = {f["key"]: f["value"] for f in store.current_facts(eid)}
    assert current == {"傷勢": "左肩中刀"}

    history = store.fact_history(eid, "傷勢")
    assert [h["value"] for h in history] == ["無", "左肩中刀"]
    assert history[0]["superseded_by"] == history[1]["id"]


def test_repeated_identical_fact_is_not_recorded_twice(store):
    eid = store.upsert_entity("老陳")
    store.set_fact(eid, "職業", "船工", turn=1)
    store.set_fact(eid, "職業", "船工", turn=2)
    assert len(store.fact_history(eid, "職業")) == 1


def test_relations_supersede(store):
    a = store.upsert_entity("掌櫃")
    b = store.upsert_entity("老陳")
    store.set_relation(a, b, "信任", strength=2, turn=1)
    store.set_relation(a, b, "信任", strength=-2, note="他說了謊", turn=9)
    current = store.current_relations()
    assert len(current) == 1
    assert current[0]["strength"] == -2
    assert current[0]["note"] == "他說了謊"


def test_relation_strength_is_clamped(store):
    a, b = store.upsert_entity("A"), store.upsert_entity("B")
    store.set_relation(a, b, "敵對", strength=-99, turn=1)
    assert store.current_relations()[0]["strength"] == -3


def test_event_participants_are_autocreated(store):
    store.record_turn(1, "x", "y")
    eid = store.add_event("掌櫃把鑰匙交給老陳", participants=["掌櫃", "老陳"], turn=1)
    assert set(store.event_participants(eid)) == {"掌櫃", "老陳"}
    assert store.resolve_entity("老陳") is not None


def test_mentioned_entities_matches_aliases(store):
    store.upsert_entity("老陳", aliases=["陳掌櫃"])
    store.upsert_entity("鏽燈街", kind="location")
    found = set(store.mentioned_entities("我沿著鏽燈街去找陳掌櫃"))
    assert len(found) == 2


def test_empty_event_summary_is_rejected(store):
    with pytest.raises(ValueError):
        store.add_event("   ")


def test_state_snapshot_shape(store):
    eid = store.upsert_entity("老陳", kind="character", summary="船工")
    store.set_fact(eid, "職業", "船工")
    store.set_flag("地窖門", "已開啟")
    snap = store.state_snapshot()
    assert snap["flags"] == {"地窖門": "已開啟"}
    assert snap["entities"][0]["facts"] == {"職業": "船工"}
