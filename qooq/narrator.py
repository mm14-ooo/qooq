"""Narration and chapter-writing calls."""

from __future__ import annotations

from typing import Any, Callable

from .config import EngineConfig
from .llm import QooqError


def narrate(
    client,
    system: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    config: EngineConfig,
    on_text: Callable[[str], None] | None = None,
) -> tuple[str, Any]:
    """Stream one turn of narration.

    Streaming is not just for the typewriter effect — it keeps a long, high-effort
    generation from tripping the SDK's request timeout.
    """
    with client.messages.stream(
        model=config.narrator_model,
        max_tokens=config.narration_max_tokens,
        system=system,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": config.narrator_effort},
    ) as stream:
        for chunk in stream.text_stream:
            if on_text:
                on_text(chunk)
        final = stream.get_final_message()

    if final.stop_reason == "refusal":
        detail = getattr(final, "stop_details", None)
        category = getattr(detail, "category", None) if detail else None
        raise QooqError(
            "敘事模型拒絕了這次生成"
            + (f"（類別：{category}）" if category else "")
            + "。請換一個行動描述再試。"
        )

    text = "".join(b.text for b in final.content if b.type == "text").strip()
    if not text:
        raise QooqError("模型沒有回傳任何敘述文字。")
    return text, final.usage


CHAPTER_SYSTEM = """\
你是一部長篇互動小說的編輯。你會收到一段時間內發生過的事件清單，
要把它壓縮成一段「前情提要」，供敘事引擎日後參考。

要求：
- 300 字以內，連貫的散文，不是條列。
- 保留一切日後可能被回頭引用的東西：承諾、債務、傷害、獲得或失去的物品、
  角色動機的轉變、尚未解答的問題。
- 捨棄純氛圍描寫與已經結束且無後續影響的細節。
- 用具體名詞，不用代名詞。
- 另外給這段時期一個 12 字以內的標題。
"""


def write_chapter(
    client, config: EngineConfig, events: list, from_turn: int, to_turn: int
) -> tuple[str, str]:
    """Compress a run of events into `(title, summary)`."""
    listing = "\n".join(
        f"- [第{e['turn']}回合｜重要度{e['importance']}] {e['summary']}" for e in events
    )
    prompt = (
        f"以下是第 {from_turn}–{to_turn} 回合之間發生的事件：\n\n{listing}\n\n"
        "請輸出前情提要。第一行是標題，之後空一行，再寫散文摘要。"
    )
    with client.messages.stream(
        model=config.editor_model,
        max_tokens=config.editor_max_tokens,
        messages=[{"role": "user", "content": prompt}],
        system=CHAPTER_SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
    ) as stream:
        final = stream.get_final_message()

    text = "".join(b.text for b in final.content if b.type == "text").strip()
    if not text:
        raise QooqError("編輯模型沒有回傳章節摘要。")
    head, _, body = text.partition("\n")
    title = head.strip().lstrip("#").strip() or f"第 {from_turn}–{to_turn} 回合"
    summary = body.strip() or text
    return title, summary
