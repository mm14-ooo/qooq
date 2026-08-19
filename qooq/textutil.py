"""Tokenisation shared by the FTS index and the retrieval query.

SQLite's built-in tokenisers are a poor fit for a bilingual story: `unicode61`
treats a whole run of Han characters as one token, and `trigram` makes ranking
noisy for Latin text.  So we tokenise in Python and feed FTS5 a pre-segmented
`search_text` column, using the same function on both the write and the read
side.

CJK is emitted as single characters *and* adjacent bigrams; bigrams carry most
of the meaning in Chinese, single characters keep partial matches alive.
"""

from __future__ import annotations

import re
import unicodedata

_LATIN = re.compile(r"[a-z0-9]+")

# Han, Hiragana, Katakana, Hangul.
_CJK_RANGES = (
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF),
)


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> list[str]:
    """Split mixed Chinese/English text into FTS-friendly tokens."""
    if not text:
        return []
    text = unicodedata.normalize("NFKC", text).lower()

    out: list[str] = []
    run: list[str] = []

    def flush_cjk() -> None:
        if not run:
            return
        out.extend(run)
        out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
        run.clear()

    buf: list[str] = []
    for ch in text:
        if is_cjk(ch):
            if buf:
                out.extend(_LATIN.findall("".join(buf)))
                buf.clear()
            run.append(ch)
        else:
            flush_cjk()
            buf.append(ch)
    flush_cjk()
    if buf:
        out.extend(_LATIN.findall("".join(buf)))

    return [t for t in out if t]


def search_text(text: str) -> str:
    """The value stored in (and queried against) the FTS column."""
    return " ".join(tokenize(text))


def fts_query(text: str, limit: int = 60) -> str:
    """Build an FTS5 OR-query from free text.

    Tokens are quoted so that punctuation left over from user input can never be
    read as FTS5 operator syntax.
    """
    toks = tokenize(text)
    if not toks:
        return ""
    seen: list[str] = []
    for t in toks:
        if t not in seen:
            seen.append(t)
        if len(seen) >= limit:
            break
    return " OR ".join('"%s"' % t.replace('"', "") for t in seen)
