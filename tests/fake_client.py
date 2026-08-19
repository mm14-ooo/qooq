"""A stand-in for anthropic.Anthropic that records requests and replays scripts.

Lets the whole turn pipeline — recall, prompt assembly, narration, extraction,
consolidation — be tested without network access or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from qooq.archivist import MemoryDelta


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 50


class _Stream:
    def __init__(self, text: str):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        # Chunked, so tests exercise the incremental-output path.
        for i in range(0, len(self._text), 7):
            yield self._text[i : i + 7]

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            stop_reason="end_turn",
            stop_details=None,
            usage=Usage(),
        )


class _Messages:
    def __init__(self, owner: "FakeClient"):
        self.owner = owner

    def stream(self, **kwargs):
        self.owner.stream_calls.append(kwargs)
        return _Stream(self.owner.next_narration())

    def parse(self, **kwargs):
        self.owner.parse_calls.append(kwargs)
        return SimpleNamespace(parsed_output=self.owner.next_delta())


@dataclass
class FakeClient:
    narrations: list[str] = field(default_factory=list)
    deltas: list[MemoryDelta | None] = field(default_factory=list)
    stream_calls: list[dict[str, Any]] = field(default_factory=list)
    parse_calls: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self):
        self.messages = _Messages(self)

    def next_narration(self) -> str:
        return self.narrations.pop(0) if self.narrations else "什麼也沒有發生。"

    def next_delta(self) -> MemoryDelta | None:
        if self.deltas:
            return self.deltas.pop(0)
        return MemoryDelta(
            scene_location="", events=[], entities=[], relations=[], flags=[]
        )


def delta(
    *,
    location: str = "",
    events: list[dict] | None = None,
    entities: list[dict] | None = None,
    relations: list[dict] | None = None,
    flags: list[dict] | None = None,
) -> MemoryDelta:
    """Build a MemoryDelta from loose dicts, filling in required fields."""
    return MemoryDelta(
        scene_location=location,
        events=[
            {
                "summary": "", "kind": "event", "importance": 2,
                "location": "", "participants": [], **e,
            }
            for e in (events or [])
        ],
        entities=[
            {
                "name": "", "kind": "character", "summary": "", "status": "",
                "importance": 3, "aliases": [], "facts": [], **e,
            }
            for e in (entities or [])
        ],
        relations=[
            {"source": "", "target": "", "kind": "", "strength": 0, "note": "", **r}
            for r in (relations or [])
        ],
        flags=[{"key": "", "value": "", **f} for f in (flags or [])],
    )
