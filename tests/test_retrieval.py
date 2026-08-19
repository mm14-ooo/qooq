import pytest

from qooq import db
from qooq.config import RetrievalWeights
from qooq.memory import MemoryStore
from qooq.textutil import fts_query, tokenize


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(db.connect(tmp_path / "s.db"))
    for n in range(1, 41):
        s.record_turn(n, f"行動{n}", f"敘述{n}")
    return s


def test_tokenizer_handles_mixed_scripts():
    toks = tokenize("老陳 in the Rusted Lantern")
    assert "老陳" in toks           # CJK bigram
    assert "陳" in toks             # CJK unigram
    assert "rusted" in toks         # lowercased latin word


def test_fts_query_quotes_tokens_so_punctuation_cannot_inject_syntax():
    q = fts_query('鑰匙" OR NEAR(')
    assert q.startswith('"')
    assert "NEAR" not in q.replace('"near"', "")


def test_lexical_match_finds_chinese_topic(store):
    store.add_event("掌櫃在碼頭撿到一枚銅鑰匙", turn=3, importance=2)
    store.add_event("雨水從屋簷滴落，街上空無一人", turn=4, importance=1)
    top = store.retrieve("那枚鑰匙後來怎麼了", limit=1)
    assert top[0].summary.endswith("銅鑰匙")


def test_entity_overlap_beats_recency(store):
    old = store.add_event("老陳承諾會替掌櫃保守秘密", turn=2, importance=3,
                          participants=["老陳", "掌櫃"])
    for n in range(30, 39):
        store.add_event(f"街上傳來腳步聲（{n}）", turn=n, importance=1)

    chen = store.resolve_entity("老陳")["id"]
    results = store.retrieve("他還記得那個約定嗎", limit=3, focus_entities=[chen])
    assert old in [r.id for r in results]


def test_high_importance_memory_stays_reachable_after_many_turns(store):
    critical = store.add_event("掌櫃永久失去了母親的臉", turn=1, importance=5)
    for n in range(5, 40):
        store.add_event(f"日常瑣事{n}", turn=n, importance=1)
    results = store.retrieve("完全無關的查詢字串", limit=5)
    assert critical in [r.id for r in results]


def test_recent_turns_can_be_excluded_from_recall(store):
    store.add_event("剛剛才發生的事", turn=40, importance=5)
    store.add_event("很久以前的事", turn=2, importance=5)
    results = store.retrieve("事", limit=10, exclude_recent_turns=4)
    assert all(r.turn <= 36 for r in results)


def test_consolidated_events_leave_the_retrieval_pool(store):
    eid = store.add_event("掌櫃在碼頭撿到一枚銅鑰匙", turn=3, importance=4)
    assert store.retrieve("銅鑰匙", limit=5)
    store.add_chapter("序章", "摘要", 1, 5, [eid])
    assert all(r.id != eid for r in store.retrieve("銅鑰匙", limit=5))


def test_weights_change_the_ranking(store):
    store.add_event("很久以前掌櫃收到一封信", turn=1, importance=5)
    store.add_event("剛才有人敲門", turn=39, importance=1)

    store.weights = RetrievalWeights(lexical=0, entity=0, importance=0, recency=1)
    assert store.retrieve("任意", limit=1)[0].turn == 39

    store.weights = RetrievalWeights(lexical=0, entity=0, importance=1, recency=0)
    assert store.retrieve("任意", limit=1)[0].turn == 1


def test_score_parts_are_reported_for_debugging(store):
    store.add_event("掌櫃在碼頭撿到一枚銅鑰匙", turn=3, importance=4, location="碼頭")
    r = store.retrieve("銅鑰匙", limit=1, location="碼頭")[0]
    assert r.parts["lexical"] > 0
    assert r.parts["location"] > 0
    assert abs(sum(r.parts.values()) - r.score) < 1e-9
