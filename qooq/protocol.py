"""The paste protocol: what we ask the AI for, and how we read its reply back.

There is no API in this system.  The app renders a prompt, a human pastes it
into whatever chat AI they use, and pastes the reply back.  So this module owns
both halves of that contract:

* :func:`build_prompt` — one prompt that asks for prose *and* a memory record.
* :func:`parse_response` — pulls the two apart again, tolerantly.

Tolerance is the whole game here.  A real chat model will fence the JSON, or
not; label the fence `json`, or not; add a friendly sentence after it; use
Chinese full-width punctuation inside strings; leave a trailing comma.  None of
that should cost the player their turn, so the parser degrades in stages and
never raises on bad JSON — it returns the prose plus a warning.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

RECORD_SCHEMA = """\
{
  "scene": "這一回合結束時主角所在的地點；不確定就給空字串",
  "events": [
    {
      "summary": "一句話說明發生了什麼。用具體名詞，不要用「他」「那個東西」",
      "importance": 3,
      "participants": ["涉及的角色或事物名稱"],
      "location": "發生地點，不確定就留空"
    }
  ],
  "entities": [
    {
      "name": "正式名稱，沿用既有紀錄的寫法",
      "kind": "character | location | item | faction | concept",
      "summary": "一句話描述，沒有新資訊就留空",
      "status": "active | absent | dead | destroyed | unknown，沒變就留空",
      "importance": 3,
      "aliases": ["別名或綽號"],
      "facts": {"屬性名": "屬性值"}
    }
  ],
  "relations": [
    {"from": "誰", "to": "對誰", "kind": "信任 / 虧欠 / 敵對 …", "strength": 1, "note": ""}
  ],
  "flags": {"劇情狀態名": "狀態值"}
}"""

RECORD_RULES = """\
- importance 是 1–5：1 是氛圍細節，2 是一般互動，3 是有後果的行動，
  4 是劇情轉折，5 是不可逆的重大事件。全部給高分等於沒有排序。
- 只寫這一回合「新出現或改變」的東西。已經在紀錄裡且沒變的不要重複。
- 事件摘要日後會被單獨讀到，沒有上下文，所以要寫成獨立可讀的句子。
- 名稱沿用既有紀錄的寫法，同一個角色不要出現第二種稱呼；別名放進 aliases。
- 沒有內容的欄位給空陣列或空物件，不要編造。"""


@dataclass
class Record:
    """A parsed memory record.  Every field defaults to empty and is validated
    loosely — a partially-broken record is more useful than none."""

    scene: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    relations: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.events or self.entities or self.relations or self.flags)


@dataclass
class ParsedResponse:
    prose: str
    record: Record | None
    warning: str = ""


# --------------------------------------------------------------- prompt side


def build_prompt(
    *,
    craft: str,
    world: str,
    briefing: str,
    recent: list[tuple[str, str]],
    action: str,
    language: str = "zh-Hant",
) -> str:
    """Assemble the single block of text the player pastes into their AI.

    Order matters: rules first, then the world, then what has happened, then
    what the player just did, and the output contract last — chat models weight
    the end of a long prompt heavily, and the output format is the instruction
    most often dropped.
    """
    parts = [craft.strip(), "", world.strip()]

    if language:
        parts += ["", f"## 語言", f"以 {language} 書寫敘述。"]

    if briefing.strip():
        parts += ["", "---", "", briefing.strip()]

    if recent:
        parts += ["", "---", "", "## 最近的敘述（逐字）"]
        for player_input, narration in recent:
            parts += ["", f"> 玩家：{player_input}", "", narration.strip()]

    parts += [
        "",
        "---",
        "",
        "## 玩家這一回合的行動",
        "",
        action.strip(),
        "",
        "---",
        "",
        "## 輸出格式（務必遵守）",
        "",
        "先寫這一回合的敘述散文。",
        "散文結束後空一行，接一個 ```json 程式碼區塊，裡面是這一回合的記憶紀錄：",
        "",
        "```json",
        RECORD_SCHEMA,
        "```",
        "",
        RECORD_RULES,
        "",
        "散文之外不要寫任何說明或評論，JSON 區塊後面也不要再補話。",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------- parse side

_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def _repair(text: str) -> str:
    """Fix the JSON mistakes chat models actually make, and nothing else."""
    # Trailing commas before a closing brace/bracket.
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    # Full-width punctuation used as JSON syntax (only outside string values is
    # hard to know, so only convert the unambiguous structural cases).
    text = re.sub(r"“|”", '"', text)
    return text


def _try_json(blob: str) -> dict | None:
    for candidate in (blob, _repair(blob)):
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _balanced_objects(text: str) -> list[str]:
    """Every top-level {...} region, in order of appearance.

    A hand-rolled scan rather than a regex, because the record contains nested
    objects and quoted braces are legal inside strings.
    """
    out: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    out.append(text[start : i + 1])
    return out


def _coerce_record(data: dict) -> Record:
    """Accept the shapes a model plausibly emits; normalise to one shape."""

    def as_list(value) -> list:
        if isinstance(value, list):
            return [v for v in value if isinstance(v, dict)]
        if isinstance(value, dict):
            return [value]
        return []

    def as_str_map(value) -> dict[str, str]:
        if isinstance(value, dict):
            return {str(k): str(v) for k, v in value.items() if str(k).strip()}
        # Some models emit [{"key": ..., "value": ...}] instead of an object.
        out: dict[str, str] = {}
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and "key" in item:
                    out[str(item["key"])] = str(item.get("value", ""))
        return out

    entities = []
    for e in as_list(data.get("entities")):
        e = dict(e)
        e["facts"] = as_str_map(e.get("facts"))
        aliases = e.get("aliases")
        e["aliases"] = [str(a) for a in aliases if str(a).strip()] if isinstance(aliases, list) else []
        entities.append(e)

    return Record(
        scene=str(data.get("scene") or data.get("location") or "").strip(),
        events=as_list(data.get("events")),
        entities=entities,
        relations=as_list(data.get("relations")),
        flags=as_str_map(data.get("flags")),
    )


def parse_response(text: str) -> ParsedResponse:
    """Split a pasted reply into prose and memory record.

    Degrades in stages: fenced JSON block → any fenced block that parses →
    trailing bare `{...}` → prose only, with a warning.
    """
    text = (text or "").strip()
    if not text:
        return ParsedResponse(prose="", record=None, warning="貼上的內容是空的。")

    # Stage 1 & 2: fenced blocks, preferring the last one that parses as an object.
    for want_json_tag in (True, False):
        for match in reversed(list(_FENCE.finditer(text))):
            tag, body = match.group(1).lower(), match.group(2)
            if want_json_tag and tag not in ("json", "json5"):
                continue
            data = _try_json(body)
            if data is not None:
                prose = text[: match.start()].strip()
                trailer = text[match.end() :].strip()
                warning = "" if not trailer else "JSON 區塊後面還有多餘文字，已忽略。"
                return ParsedResponse(prose=prose, record=_coerce_record(data), warning=warning)

    # Stage 3: an unfenced object, taking the last one that looks like a record.
    for blob in reversed(_balanced_objects(text)):
        data = _try_json(blob)
        if data is None:
            continue
        if not any(k in data for k in ("events", "entities", "relations", "flags", "scene")):
            continue
        index = text.rfind(blob)
        return ParsedResponse(
            prose=text[:index].strip(),
            record=_coerce_record(data),
            warning="JSON 沒有放在程式碼區塊裡，已自動辨識。",
        )

    # Stage 4: prose only.  The turn still counts.
    return ParsedResponse(
        prose=text,
        record=None,
        warning="找不到 JSON 記憶紀錄，這一回合的散文已保存，但沒有寫入長期記憶。",
    )


# ------------------------------------------------------------ consolidation

CHAPTER_CRAFT = """\
你是一部長篇互動小說的編輯。下面是一段時期內發生過的事件清單，
請把它壓縮成一段「前情提要」，供日後的敘述參考。

要求：
- 300 字以內，連貫的散文，不是條列。
- 保留一切日後可能被回頭引用的東西：承諾、債務、傷害、獲得或失去的物品、
  角色動機的轉變、尚未解答的問題。
- 捨棄純氛圍描寫，以及已經結束且不再有後續影響的細節。
- 用具體名詞，不要用代名詞。"""


def build_chapter_prompt(listing: str, from_turn: int, to_turn: int) -> str:
    return "\n".join(
        [
            CHAPTER_CRAFT,
            "",
            "---",
            "",
            f"## 第 {from_turn}–{to_turn} 回合的事件",
            "",
            listing,
            "",
            "---",
            "",
            "## 輸出格式",
            "",
            "第一行是這段時期的標題（12 字以內，不加標點或符號）。",
            "空一行，然後是散文摘要。除此之外不要寫別的。",
        ]
    )


def parse_chapter(text: str) -> tuple[str, str]:
    """Split a pasted chapter reply into `(title, summary)`."""
    text = (text or "").strip()
    if not text:
        raise ValueError("貼上的內容是空的")
    head, _, body = text.partition("\n")
    title = head.strip().lstrip("#＃").strip().strip("《》「」") or "前情提要"
    summary = body.strip() or text
    return title, summary
