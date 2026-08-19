"""Prompt assembly — deciding what the narrator is allowed to remember.

The layout is deliberate:

  system   : narrator craft + world bible          (stable → prompt-cached)
  messages : the last few turns, verbatim          (voice and pronoun continuity)
  briefing : state + chapters + retrieved memories (volatile, rebuilt each turn)
  action   : what the player just typed            (untrusted input)

On models that support mid-conversation system messages the briefing is sent as
`role: "system"`, which keeps it on the operator channel — the player cannot
type something that looks like a memory record and have it treated as canon.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .config import EngineConfig
from .memory import MemoryStore, RetrievedEvent
from .world import World

# Models that accept `{"role": "system"}` inside `messages`.
_SYSTEM_MESSAGE_MODELS = ("claude-opus-5", "claude-opus-4-8", "claude-fable-5", "claude-mythos-5")

NARRATOR_CRAFT = """\
你是一部互動小說的敘事引擎。玩家輸入他們角色的行動，你敘述接下來發生的事。

寫作要求：
- 用第二人稱、過去式或現在式擇一並全篇一致，維持作品既定的文體。
- 每次回應 150–350 字，聚焦在這一個當下：感官細節、角色反應、環境變化。
- 讓世界有自己的動能。NPC 有自己的目的，不只是等玩家發問。
- 不要替玩家決定行動或內心想法，不要在結尾列出選項清單。
- 不要提及記憶系統、回合數、模型或任何遊戲機制。

記憶紀律（最重要的一條）：
- 下方的「當前狀態」「往事」「前情提要」是這個故事已經發生過的事實。
- 與它們矛盾的內容一律不得寫出。角色記得他們記得的事，欠的人情仍然欠著。
- 若某件事在紀錄中沒有寫明，你可以自由創造，但一經寫出就會成為新的事實。
- 當玩家提起過去的人事物，優先使用紀錄中的既有細節，而不是重新編造。
"""


def _render_state(state: dict[str, Any]) -> str:
    lines: list[str] = []
    entities = state.get("entities") or []
    if entities:
        lines.append("## 當前狀態")
        by_kind: dict[str, list[dict]] = {}
        for e in entities:
            by_kind.setdefault(e["kind"], []).append(e)
        labels = {
            "character": "人物",
            "location": "地點",
            "item": "物品",
            "faction": "勢力",
            "concept": "概念",
        }
        for kind in ("character", "location", "item", "faction", "concept"):
            group = by_kind.get(kind)
            if not group:
                continue
            lines.append(f"### {labels[kind]}")
            for e in group:
                head = f"- {e['name']}"
                if e["status"] and e["status"] != "active":
                    head += f"（{e['status']}）"
                if e["summary"]:
                    head += f"：{e['summary']}"
                lines.append(head)
                for key, value in e["facts"].items():
                    lines.append(f"    - {key}：{value}")

    relations = state.get("relations") or []
    if relations:
        lines.append("### 關係")
        for r in relations:
            note = f"（{r['note']}）" if r["note"] else ""
            lines.append(f"- {r['src']} → {r['dst']}：{r['kind']} [{r['strength']:+d}]{note}")

    flags = state.get("flags") or {}
    if flags:
        lines.append("### 劇情旗標")
        for key, value in flags.items():
            lines.append(f"- {key} = {value}")
    return "\n".join(lines)


def _render_memories(memories: list[RetrievedEvent]) -> str:
    if not memories:
        return ""
    lines = ["## 往事（依與此刻的相關性取出）"]
    for m in memories:
        who = f"　參與者：{'、'.join(m.participants)}" if m.participants else ""
        where = f"　地點：{m.location}" if m.location else ""
        lines.append(f"- [第{m.turn}回合｜重要度{m.importance}] {m.summary}{who}{where}")
    return "\n".join(lines)


def _render_chapters(chapters: list[sqlite3.Row]) -> str:
    if not chapters:
        return ""
    lines = ["## 前情提要"]
    for c in chapters:
        lines.append(f"### {c['title']}（第{c['from_turn']}–{c['to_turn']}回合）")
        lines.append(c["summary"].strip())
    return "\n".join(lines)


def supports_system_messages(model: str) -> bool:
    return any(model.startswith(m) for m in _SYSTEM_MESSAGE_MODELS)


def build_system(world: World, config: EngineConfig) -> list[dict[str, Any]]:
    """The stable prefix.  Cached, so it must not contain per-turn content."""
    text = NARRATOR_CRAFT + "\n\n" + world.render()
    if world.language:
        text += f"\n\n## 語言\n以 {world.language} 書寫敘述。"
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def build_briefing(
    store: MemoryStore, config: EngineConfig, memories: list[RetrievedEvent]
) -> str:
    parts = [
        _render_chapters(store.chapters()),
        _render_state(store.state_snapshot(config.max_state_entities)),
        _render_memories(memories),
    ]
    body = "\n\n".join(p for p in parts if p)
    return "以下是這個故事到目前為止的紀錄，敘述時必須與之一致。\n\n" + body


def build_turn_request(
    store: MemoryStore,
    world: World,
    config: EngineConfig,
    player_input: str,
    memories: list[RetrievedEvent],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(system, messages)` for one narration request."""
    system = build_system(world, config)
    messages: list[dict[str, Any]] = []

    for row in store.recent_turns(config.verbatim_turns):
        messages.append({"role": "user", "content": row["player_input"]})
        messages.append({"role": "assistant", "content": row["narration"]})

    briefing = build_briefing(store, config, memories)
    action = f"【玩家行動】{player_input.strip()}"

    if supports_system_messages(config.narrator_model):
        # Operator channel: memory is engine-authored, the action is not.
        messages.append({"role": "user", "content": action})
        messages.append({"role": "system", "content": briefing})
    else:
        messages.append({"role": "user", "content": f"{briefing}\n\n{action}"})

    if not messages or messages[0]["role"] != "user":
        messages.insert(0, {"role": "user", "content": world.opening or "（故事開始）"})
    return system, messages
