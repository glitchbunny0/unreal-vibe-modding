"""The command-line half every app shares: printing a request's result, the status report, and the subcommands every
app's CLI has. The app's own cli.py adds its commands and its help text."""

from __future__ import annotations

import json
import sys
import time

from . import journal, protocol

EXIT_OK, EXIT_LUA_ERROR, EXIT_USAGE, EXIT_NO_ANSWER, EXIT_NOT_MET = 0, 1, 2, 3, 4

GENERIC_COMMANDS = ("lua", "run", "mods", "status", "log", "follow", "push", "launch", "stop", "restart", "ready",
                    "crash", "journal")


def print_result(session, result, as_json: bool = False) -> int:
    if result.answer is None:
        print(session.no_answer(result.delivery))
        return EXIT_NO_ANSWER
    if as_json:
        print(json.dumps(dict(result.answer, delivery=result.delivery), ensure_ascii=False))
    else:
        print(protocol.render_text(result.answer))
    return EXIT_OK if result.answer.get("ok") else EXIT_LUA_ERROR


def status_text(session) -> tuple[str, int]:
    """The status report: the text and its exit code. The lines that name the app's engine or loader come from
    session.status_texts(st) and session.bridge_facts() (asked only when there is a heartbeat); the generic ones (the game and its log, the VibeBridge heartbeat, the REPL and the mod
    that will run code) come from here."""
    st = session.status()
    texts = session.status_texts(st)
    prefix = session.settings.app.prefix
    out = []
    if st.running is None:
        out.append(texts.get("game_unknown") or f"game: unknown (set {prefix}_PROCESS)")
    elif st.running:
        out.append(f"game: running ({st.process}, pid {st.pid}" + (f", started {st.started}" if st.started else "") + ")")
    else:
        out.append(f"game: not running ({st.process})")
    if st.log_time:
        out.append(f"log: {st.log_time}")
        for line in st.log_lines:
            out.append(f"  {line}")
    out += texts.get("before_bridge", [])
    if st.bridge_age is not None:
        state = "running" if st.bridge_age <= protocol.BRIDGE_FRESH_SECONDS else f"not running (last heartbeat {st.bridge_age:.0f} s ago)"
        facts = session.bridge_facts()
        if facts:
            state += f" ({', '.join(facts)})"
        out.append(f"VibeBridge: {state}")
    else:
        out.append(f"VibeBridge: {'installed, no heartbeat yet' if st.bridge_installed else 'not installed'}")
    out += texts.get("after_bridge", [])
    if not st.repl_folder:
        out.append(texts.get("repl_missing") or "repl: no LuaRepl folder")
        return "\n".join(out), EXIT_NO_ANSWER
    saved, session.settings.timeout = session.settings.timeout, min(session.settings.timeout, 3.0)
    try:
        listed = session.mods()
    finally:
        session.settings.timeout = saved
    if listed.answer and listed.answer.get("ok"):
        who = "VibeBridge" if listed.delivery == "bridge" else f"{session.transport.engine.label} external REPL"
        out.append(f"repl: {who} answering in {listed.elapsed_s * 1000:.0f} ms; mods: {', '.join(listed.answer['mods'])}")
        mods = listed.answer["mods"]
        if session.settings.mod:
            target = session.settings.mod + ("" if session.settings.mod in mods else "  <- not among the mods above")
        elif len(mods) == 1:
            target = f"{mods[0]} (the only mod)"
        else:
            target = f"none set: pass --mod or set {prefix}_MOD to one of the mods above"
        out.append(f"target: {target}")
        return "\n".join(out), EXIT_OK
    out.append(f"repl: not answering ({session.no_answer(listed.delivery, 3.0)})")
    return "\n".join(out), EXIT_NO_ANSWER


def add_generic(sub) -> None:
    """Add the subcommands every app has (GENERIC_COMMANDS) to an argparse subparser group."""
    for name in ("lua", "run", "mods"):
        p = sub.add_parser(name)
        if name == "lua":
            p.add_argument("code")
        if name == "run":
            p.add_argument("file", nargs="?", default="-")
        p.add_argument("--mod")
        p.add_argument("--limit", type=int)
        p.add_argument("--json", action="store_true")
    sub.add_parser("status")
    p = sub.add_parser("log")
    p.add_argument("lines", nargs="?", type=int, default=40)
    p.add_argument("pattern", nargs="?", default="")
    p = sub.add_parser("follow")
    p.add_argument("pattern", nargs="?", default="")
    sub.add_parser("push").add_argument("dir")
    sub.add_parser("launch")
    sub.add_parser("stop")
    for name in ("ready", "restart"):
        sub.add_parser(name).add_argument("--timeout", type=float, default=180.0)
    sub.add_parser("crash")
    sub.add_parser("journal").add_argument("count", nargs="?", type=int, default=10)


def run_generic(session, args) -> int:
    """Run one of the generic commands against the session. The app's cli.py calls this before its own commands."""
    if args.cmd in ("lua", "run"):
        code = args.code if args.cmd == "lua" else (sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read())
        return print_result(session, session.exec(code, args.mod, args.limit), args.json)
    if args.cmd == "mods":
        return print_result(session, session.mods(), args.json)
    if args.cmd == "status":
        body, rc = status_text(session)
        print(body)
        return rc
    if args.cmd == "log":
        print("\n".join(session.log(args.lines, args.pattern)))
        return EXIT_OK
    if args.cmd == "follow":
        try:
            for line in session.follow(args.pattern):
                print(line, flush=True)
        except KeyboardInterrupt:
            pass
        return EXIT_OK
    if args.cmd == "push":
        print(session.push(args.dir))
        return EXIT_OK
    if args.cmd == "launch":
        print(session.launch())
        return EXIT_OK
    if args.cmd == "stop":
        print(session.stop())
        return EXIT_OK
    if args.cmd in ("ready", "restart"):
        if args.cmd == "restart":
            print(session.stop())
            time.sleep(2.0)
            print(session.launch())
            time.sleep(10.0)  # the old log and LuaRepl folder are still there for a few seconds
        ok, text = session.ready(args.timeout)
        print(text)
        return EXIT_OK if ok else EXIT_NO_ANSWER
    if args.cmd == "crash":
        print(session.crash_report())
        return EXIT_OK
    print(journal.render(journal.tail(session.settings, args.count)))
    return EXIT_OK
