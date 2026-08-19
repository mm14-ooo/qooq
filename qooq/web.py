"""A local web app for the paste workflow.

Deliberately stdlib-only: this app never makes a network call of its own, so
adding a web framework to serve one page and eight JSON endpoints would be the
only dependency in the project.  `python3 -m qooq.web mystory` and open a tab.

Single-player, bound to localhost.  One save file per server process.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import EngineConfig
from .engine import Engine

STATIC = Path(__file__).parent / "static"


def _rows(rows) -> list[dict]:
    return [dict(r) if isinstance(r, sqlite3.Row) else r for r in rows]


class Handler(BaseHTTPRequestHandler):
    server_version = "qooq"

    def __init__(self, *args, engine: Engine, lock: threading.Lock, **kwargs):
        self.engine = engine
        self.lock = lock
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # -------------------------------------------------------------- routing

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_bytes()
            return self._send(200, html, "text/html; charset=utf-8")

        with self.lock:
            try:
                if url.path == "/api/state":
                    return self._json(self._state())
                if url.path == "/api/memory":
                    return self._json(self._memory())
                if url.path == "/api/log":
                    limit = int((query.get("limit") or ["50"])[0])
                    return self._json({"turns": _rows(self.engine.store.recent_turns(limit))})
                if url.path == "/api/recall":
                    q = (query.get("q") or [""])[0]
                    return self._json({"results": self._recall(q)})
                if url.path == "/api/chapter-prompt":
                    return self._json({"prompt": self.engine.consolidation_prompt()})
            except Exception as exc:
                return self._json({"error": str(exc)}, 400)
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        payload = self._body()

        with self.lock:
            try:
                if url.path == "/api/prompt":
                    pending = self.engine.begin_turn(payload.get("action", ""))
                    return self._json({
                        "action": pending.action,
                        "prompt": pending.prompt,
                        "memories": [
                            {"turn": m.turn, "summary": m.summary,
                             "importance": m.importance, "score": round(m.score, 3)}
                            for m in pending.memories
                        ],
                    })
                if url.path == "/api/ingest":
                    result = self.engine.ingest(payload.get("reply", ""))
                    return self._json({
                        "turn": result.turn,
                        "narration": result.narration,
                        "written": result.written,
                        "warning": result.warning,
                        "state": self._state(),
                    })
                if url.path == "/api/cancel":
                    self.engine.cancel_pending()
                    return self._json({"ok": True})
                if url.path == "/api/chapter":
                    return self._json({
                        "message": self.engine.ingest_chapter(payload.get("reply", "")),
                        "state": self._state(),
                    })
            except Exception as exc:
                return self._json({"error": str(exc)}, 400)
        self._json({"error": "not found"}, 404)

    # ----------------------------------------------------------- view models

    def _state(self) -> dict:
        store = self.engine.store
        return {
            "title": self.engine.world.title,
            "turn": store.turn,
            "pending": self.engine.pending_action(),
            "consolidation_due": self.engine.consolidation_due(),
            "stats": store.stats(),
            "transcript": _rows(store.recent_turns(self.engine.config.verbatim_turns + 6)),
        }

    def _memory(self) -> dict:
        store = self.engine.store
        snapshot = store.state_snapshot()
        events = store.conn.execute(
            "SELECT * FROM events WHERE consolidated_into IS NULL"
            " ORDER BY turn DESC, id DESC LIMIT 40"
        ).fetchall()
        return {
            **snapshot,
            "chapters": _rows(store.chapters()),
            "events": [
                {**dict(e), "participants": store.event_participants(int(e["id"]))}
                for e in events
            ],
        }

    def _recall(self, query: str) -> list[dict]:
        if not query.strip():
            return []
        store = self.engine.store
        results = store.retrieve(
            query, limit=12, focus_entities=store.mentioned_entities(query)
        )
        return [
            {
                "turn": r.turn, "summary": r.summary, "importance": r.importance,
                "score": round(r.score, 3),
                "parts": {k: round(v, 3) for k, v in r.parts.items() if v},
            }
            for r in results
        ]


def serve(save: str | Path, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, config: EngineConfig | None = None) -> None:
    # Every request body runs under `lock`, so the connection may cross threads.
    engine = Engine.load(save, config, same_thread=False)
    handler = partial(Handler, engine=engine, lock=threading.Lock())
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"《{engine.world.title}》　第 {engine.store.turn} 回合")
    print(f"qooq 已啟動：{url}\n按 Ctrl-C 結束。")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已存檔，結束。")
    finally:
        httpd.server_close()
        engine.close()


if __name__ == "__main__":
    import sys

    serve(sys.argv[1] if len(sys.argv) > 1 else "saves/mystory.db")
