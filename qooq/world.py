"""The world bible — the authored, immutable half of a story.

Everything here is written once by the author and never changed by play; it is
the stable prefix of every prompt.  What the *player* changes lives in the
memory store instead.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SeedEntity:
    name: str
    kind: str = "character"
    summary: str = ""
    importance: int = 3
    aliases: list[str] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class World:
    title: str
    premise: str
    setting: str = ""
    tone: str = ""
    language: str = "zh-Hant"
    # Hard rules the narrator may never break (magic system limits, genre
    # constraints, content boundaries).
    rules: list[str] = field(default_factory=list)
    protagonist: str = ""
    opening: str = ""
    seed_entities: list[SeedEntity] = field(default_factory=list)
    seed_flags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "World":
        seeds = [SeedEntity(**s) for s in data.get("seed_entities", [])]
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in data.items() if k in known}
        payload["seed_entities"] = seeds
        return cls(**payload)

    @classmethod
    def load(cls, path: str | Path) -> "World":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def render(self) -> str:
        """The world bible as it appears in the system prompt."""
        lines = [f"# 作品：{self.title}", "", "## 前提", self.premise.strip()]
        if self.setting:
            lines += ["", "## 場景與世界觀", self.setting.strip()]
        if self.tone:
            lines += ["", "## 文體與基調", self.tone.strip()]
        if self.protagonist:
            lines += ["", "## 主角", self.protagonist.strip()]
        if self.rules:
            lines += ["", "## 不可違背的設定"]
            lines += [f"- {r}" for r in self.rules]
        return "\n".join(lines)


def bundled_world_path(name: str = "lantern") -> Path:
    return Path(__file__).parent / "worlds" / f"{name}.json"
