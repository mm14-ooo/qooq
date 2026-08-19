"""The parser has to survive whatever a chat model actually pastes back."""

import json

import pytest

from qooq.protocol import build_prompt, parse_chapter, parse_response

RECORD = {
    "scene": "當鋪",
    "events": [{"summary": "老陳冒雨走進當鋪", "importance": 3, "participants": ["老陳"]}],
    "entities": [{"name": "老陳", "kind": "character", "facts": {"狀態": "全身濕透"}}],
    "relations": [{"from": "掌櫃", "to": "老陳", "kind": "認識", "strength": 1}],
    "flags": {"門外": "已進門"},
}
PROSE = "雨聲忽然近了。\n\n門開了一半，老陳站在那裡。"


def wrap(record, tag="json", trailer=""):
    body = json.dumps(record, ensure_ascii=False, indent=2)
    return f"{PROSE}\n\n```{tag}\n{body}\n```{trailer}"


def test_fenced_json_is_the_happy_path():
    out = parse_response(wrap(RECORD))
    assert out.prose == PROSE
    assert out.warning == ""
    assert out.record.scene == "當鋪"
    assert out.record.events[0]["summary"] == "老陳冒雨走進當鋪"
    assert out.record.flags == {"門外": "已進門"}


def test_unlabelled_fence_still_parses():
    out = parse_response(wrap(RECORD, tag=""))
    assert out.prose == PROSE
    assert out.record.entities[0]["name"] == "老陳"


def test_bare_json_without_a_fence_is_recovered():
    out = parse_response(PROSE + "\n\n" + json.dumps(RECORD, ensure_ascii=False))
    assert out.prose == PROSE
    assert out.record.scene == "當鋪"
    assert "沒有放在程式碼區塊" in out.warning


def test_chatter_after_the_json_is_dropped_but_flagged():
    out = parse_response(wrap(RECORD, trailer="\n\n希望你喜歡這一段！"))
    assert out.prose == PROSE
    assert out.record is not None
    assert "多餘文字" in out.warning


def test_trailing_comma_is_repaired():
    broken = f'{PROSE}\n\n```json\n{{"scene": "當鋪", "events": [],}}\n```'
    out = parse_response(broken)
    assert out.record is not None
    assert out.record.scene == "當鋪"


def test_prose_with_braces_does_not_confuse_the_scanner():
    prose = "他寫下 {一個符號}，然後停手。"
    out = parse_response(f"{prose}\n\n```json\n{json.dumps(RECORD, ensure_ascii=False)}\n```")
    assert out.prose == prose
    assert out.record.scene == "當鋪"


def test_json_containing_braces_inside_strings():
    record = dict(RECORD, scene='寫著 "{" 的門')
    out = parse_response(wrap(record))
    assert out.record.scene == '寫著 "{" 的門'


def test_missing_json_keeps_the_prose_and_warns():
    out = parse_response(PROSE)
    assert out.prose == PROSE
    assert out.record is None
    assert "沒有寫入長期記憶" in out.warning


def test_malformed_json_never_raises():
    out = parse_response(f"{PROSE}\n\n```json\n{{這根本不是 JSON\n```")
    assert out.prose.startswith("雨聲")
    assert out.record is None


def test_empty_paste_is_reported():
    assert parse_response("   ").record is None
    assert "空的" in parse_response("   ").warning


def test_key_value_array_shape_is_accepted_for_flags():
    record = dict(RECORD, flags=[{"key": "地窖門", "value": "已開啟"}])
    out = parse_response(wrap(record))
    assert out.record.flags == {"地窖門": "已開啟"}


def test_alternate_relation_key_names_are_accepted():
    record = dict(RECORD, relations=[{"source": "甲", "target": "乙", "kind": "敵對"}])
    out = parse_response(wrap(record))
    assert out.record.relations[0]["source"] == "甲"


def test_non_dict_junk_in_lists_is_dropped():
    record = dict(RECORD, events=["這不是物件", {"summary": "有效事件"}])
    out = parse_response(wrap(record))
    assert len(out.record.events) == 1


def test_last_json_block_wins_when_the_model_shows_the_template_first():
    text = f"格式範例：\n```json\n{{}}\n```\n\n{wrap(RECORD)}"
    out = parse_response(text)
    assert out.record.scene == "當鋪"


def test_prompt_puts_the_output_contract_last():
    prompt = build_prompt(
        craft="敘事守則", world="# 作品：測試", briefing="# 紀錄",
        recent=[("開門", "門開了。")], action="往前走",
    )
    assert prompt.index("往前走") < prompt.index("## 輸出格式")
    assert prompt.rstrip().endswith("JSON 區塊後面也不要再補話。")
    assert "門開了。" in prompt


def test_chapter_parsing_splits_title_from_body():
    title, summary = parse_chapter("《第一夜》\n\n掌櫃收下了那段記憶。")
    assert title == "第一夜"
    assert summary == "掌櫃收下了那段記憶。"


def test_chapter_without_a_blank_line_still_works():
    title, summary = parse_chapter("序章\n掌櫃收下了那段記憶。")
    assert title == "序章"
    assert "掌櫃" in summary


def test_empty_chapter_is_rejected():
    with pytest.raises(ValueError):
        parse_chapter("")
