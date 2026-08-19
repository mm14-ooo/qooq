"""Tunable settings for the engine.

Every knob that changes the *feel* of the game lives here rather than being
scattered through the prompt builders.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# The narrator carries the whole story voice, so it gets the strongest model.
NARRATOR_MODEL = os.environ.get("QOOQ_NARRATOR_MODEL", "claude-opus-5")
# The archivist turns prose into structured memory — mechanical, but getting it
# wrong quietly corrupts the story's canon, so it is not downgraded by default.
ARCHIVIST_MODEL = os.environ.get("QOOQ_ARCHIVIST_MODEL", "claude-opus-5")
# The editor writes chapter summaries during consolidation.
EDITOR_MODEL = os.environ.get("QOOQ_EDITOR_MODEL", "claude-opus-5")

SAVE_DIR = Path(os.environ.get("QOOQ_SAVE_DIR", "saves"))


@dataclass
class RetrievalWeights:
    """How the four retrieval signals trade off against each other.

    Defaults favour lexical match and entity overlap (what the player is
    actually engaging with) over raw recency, so the engine surfaces the
    promise made twenty turns ago rather than the corridor described last turn.
    """

    lexical: float = 1.0
    entity: float = 0.9
    importance: float = 0.7
    recency: float = 0.5
    location: float = 0.3
    # Turns until the recency bonus decays to ~37% of its peak.
    recency_halflife: float = 12.0


@dataclass
class EngineConfig:
    narrator_model: str = NARRATOR_MODEL
    archivist_model: str = ARCHIVIST_MODEL
    editor_model: str = EDITOR_MODEL

    # Turns of raw prose kept in the prompt for voice and pronoun continuity.
    verbatim_turns: int = 4
    # Episodic memories retrieved per turn.
    retrieved_memories: int = 8
    # Entities described in the state block.
    max_state_entities: int = 40

    # Consolidation fires once this many live events pile up, and compresses
    # everything older than `consolidate_keep` turns.
    consolidate_after_events: int = 60
    consolidate_keep_turns: int = 15

    narration_max_tokens: int = 4000
    archivist_max_tokens: int = 8000
    editor_max_tokens: int = 4000

    # Effort for the narrator. "high" is the sweet spot; "max" for set pieces.
    narrator_effort: str = "high"
    archivist_effort: str = "medium"

    weights: RetrievalWeights = field(default_factory=RetrievalWeights)
