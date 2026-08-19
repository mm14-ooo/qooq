"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import db
from .config import SAVE_DIR, EngineConfig
from .engine import Engine
from .llm import QooqError, build_client, describe_api_error
from .memory import MemoryStore
from .world import World, bundled_world_path

BANNER = """\
qooq — 有長期記憶的互動小說
輸入行動即可推進故事。指令：/memory /state /recall <關鍵字> /log /help /quit
"""


def save_path(name: str) -> Path:
    p = Path(name)
    if p.suffix == ".db" or p.parent != Path("."):
        return p
    return SAVE_DIR / f"{name}.db"


def _load_world(arg: str | None) -> World:
    if arg is None:
        return World.load(bundled_world_path("lantern"))
    candidate = Path(arg)
    if candidate.exists():
        return World.load(candidate)
    bundled = bundled_world_path(arg)
    if bundled.exists():
        return World.load(bundled)
    raise QooqError(f"找不到世界設定檔：{arg}")


# ---------------------------------------------------------------- rendering


def print_memory(store: MemoryStore, limit: int = 20) -> None:
    stats = store.stats()
    print(
        f"回合 {stats['turns']}｜實體 {stats['entities']}｜屬性 {stats['facts']}"
        f"｜關係 {stats['relations']}｜旗標 {stats['flags']}"
        f"｜事件 {stats['events_live']}/{stats['events']}｜章節 {stats['chapters']}"
    )
    state = store.state_snapshot()
    if state["entities"]:
        print("\n— 實體 —")
        for e in state["entities"]:
            print(f"  [{e['kind']}] {e['name']} ({e['status']}, 重要度 {e['importance']})")
            if e["summary"]:
                print(f"      {e['summary']}")
            for key, value in e["facts"].items():
                print(f"      · {key}：{value}")
    if state["relations"]:
        print("\n— 關係 —")
        for r in state["relations"]:
            print(f"  {r['src']} →({r['kind']} {r['strength']:+d})→ {r['dst']} {r['note']}")
    if state["flags"]:
        print("\n— 旗標 —")
        for key, value in state["flags"].items():
            print(f"  {key} = {value}")

    chapters = store.chapters()
    if chapters:
        print("\n— 前情提要 —")
        for c in chapters:
            print(f"  《{c['title']}》第{c['from_turn']}–{c['to_turn']}回合")
            print(f"    {c['summary']}")

    events = store.conn.execute(
        "SELECT * FROM events WHERE consolidated_into IS NULL"
        " ORDER BY turn DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    if events:
        print(f"\n— 最近事件（最多 {limit} 則）—")
        for e in events:
            who = "、".join(store.event_participants(int(e["id"])))
            print(f"  [第{e['turn']}回合｜重要度{e['importance']}] {e['summary']}"
                  + (f"　（{who}）" if who else ""))


def print_recall(store: MemoryStore, query: str, limit: int) -> None:
    focus = store.mentioned_entities(query)
    results = store.retrieve(query, limit=limit, focus_entities=focus)
    if not results:
        print("（沒有相關記憶）")
        return
    for r in results:
        detail = " ".join(f"{k}={v:.2f}" for k, v in r.parts.items() if v)
        print(f"{r.score:5.2f}  [第{r.turn}回合｜重要度{r.importance}] {r.summary}")
        print(f"       {detail}")


# ---------------------------------------------------------------- commands


def cmd_new(args) -> int:
    world = _load_world(args.world)
    if args.title:
        world.title = args.title
    path = save_path(args.name)
    engine = Engine.create(path, world, client=None, config=EngineConfig())
    print(f"已建立存檔：{path}")
    print(f"作品：{world.title}\n")
    print(engine.store.recent_turns(1)[0]["narration"])
    engine.close()
    return 0


def cmd_play(args) -> int:
    path = save_path(args.name)
    if not path.exists():
        print(f"找不到存檔 {path}。先執行：qooq new {args.name}", file=sys.stderr)
        return 1

    config = EngineConfig()
    if args.effort:
        config.narrator_effort = args.effort
    if args.model:
        config.narrator_model = args.model

    engine = Engine.load(path, client=build_client(), config=config)
    print(BANNER)
    print(f"《{engine.world.title}》　第 {engine.store.turn} 回合\n")
    last = engine.store.recent_turns(1)
    if last:
        print(last[-1]["narration"], "\n")

    try:
        while True:
            try:
                raw = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not raw:
                continue

            if raw.startswith("/"):
                command, _, rest = raw[1:].partition(" ")
                if command in ("quit", "q", "exit"):
                    break
                if command in ("help", "h"):
                    print(BANNER)
                elif command == "memory":
                    print_memory(engine.store)
                elif command == "state":
                    print(json.dumps(engine.store.state_snapshot(), ensure_ascii=False, indent=2))
                elif command == "recall":
                    if rest.strip():
                        print_recall(engine.store, rest.strip(), args.recall_limit)
                    else:
                        print("用法：/recall <關鍵字>")
                elif command == "log":
                    for row in engine.store.recent_turns(10):
                        print(f"\n[{row['n']}] > {row['player_input']}\n{row['narration']}")
                else:
                    print(f"未知指令 /{command}")
                print()
                continue

            try:
                result = engine.play(raw, on_text=lambda t: print(t, end="", flush=True))
            except QooqError as exc:
                print(f"\n[!] {exc}\n")
                continue
            except Exception as exc:
                print(f"\n[!] {describe_api_error(exc)}\n")
                continue

            print("\n")
            if args.verbose and result.memories:
                print("  ⟨取用記憶⟩")
                for m in result.memories:
                    print(f"    {m.score:5.2f} [第{m.turn}回合] {m.summary}")
            if args.verbose and result.written:
                written = "、".join(f"{k}{v}" for k, v in result.written.items() if v)
                print(f"  ⟨寫入記憶⟩ {written or '無'}")
            if result.consolidated:
                print(f"  ⟨壓縮章節⟩ {result.consolidated}")
            if result.warning:
                print(f"  [!] {result.warning}")
            if args.verbose:
                print()
    finally:
        engine.close()
    print("已存檔。")
    return 0


def cmd_memory(args) -> int:
    engine = Engine.load(save_path(args.name))
    print_memory(engine.store, args.limit)
    engine.close()
    return 0


def cmd_recall(args) -> int:
    engine = Engine.load(save_path(args.name))
    print_recall(engine.store, args.query, args.limit)
    engine.close()
    return 0


def cmd_log(args) -> int:
    engine = Engine.load(save_path(args.name))
    for row in engine.store.recent_turns(args.limit):
        print(f"\n[{row['n']}] > {row['player_input']}\n{row['narration']}")
    engine.close()
    return 0


def cmd_worlds(args) -> int:
    directory = Path(__file__).parent / "worlds"
    for path in sorted(directory.glob("*.json")):
        world = World.load(path)
        print(f"{path.stem:12s} {world.title} — {world.premise.splitlines()[0][:60]}")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qooq", description="有長期記憶的互動小說引擎")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("new", help="建立新存檔")
    p.add_argument("name")
    p.add_argument("--world", help="世界設定 JSON 路徑，或內建世界名稱")
    p.add_argument("--title", help="覆寫作品標題")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("play", help="開始／繼續遊玩")
    p.add_argument("name")
    p.add_argument("-v", "--verbose", action="store_true", help="顯示記憶取用與寫入")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--model", help="覆寫敘事模型")
    p.add_argument("--recall-limit", type=int, default=8)
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("memory", help="檢視長期記憶")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("recall", help="測試檢索排序")
    p.add_argument("name")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=8)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("log", help="顯示最近的敘事紀錄")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("worlds", help="列出內建世界")
    p.set_defaults(func=cmd_worlds)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except QooqError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # `qooq memory ... | head` closes the pipe early; that is not an error.
        # Redirect stdout so the interpreter's shutdown flush cannot re-raise.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
