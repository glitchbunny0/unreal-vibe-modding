"""uvmod: drive a running Unreal Engine game through UE4SS. Run `uvmod help` for the commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources

from . import __version__, protocol
from .config import KEYS, ConfigError, Settings
from .game import Game, GameError, timeout_message
from .transports import TransportError

EXIT_OK, EXIT_LUA_ERROR, EXIT_USAGE, EXIT_NO_ANSWER = 0, 1, 2, 3

HELP = f"""uvmod {__version__} — vibe modding for Unreal Engine games: run Lua live in a running game through UE4SS.

  uvmod lua 'return FindFirstOf("PlayerController"):GetFullName()'   run Lua on the game thread
  uvmod run file.lua | uvmod run -          ... from a file / stdin
  uvmod mods                                mods whose Lua state can run code
  uvmod status                              game running? which REPL answers? settings that matter
  uvmod log [N] [REGEX]                     last N lines of UE4SS.log (default 40)
  uvmod follow [REGEX]                      stream the log until Ctrl+C
  uvmod push DIR                            copy a mod folder into Mods/ (loads at the next game start)
  uvmod swap DIR                            push a hot-swap-shell mod and swap its logic in place
  uvmod shot [OUT.jpg]                      engine screenshot, copied here
  uvmod install-bridge                      install the VibeBridge mod (for UE4SS builds without the external REPL)
  uvmod ps < script.ps1                     run PowerShell on the game box (ssh transport)
  uvmod mcp                                 MCP server on stdio, for AI agents

  lua/run/mods take --mod NAME, --limit MS (1-5000) and --json (print the raw answer).
  Exit codes: 0 OK, 1 the Lua raised an error, 2 usage or settings, 3 nothing answered.

Settings come from the environment or from the config file (KEY=value lines; the environment wins):
""" + "\n".join(f"  {k:<16} {v}" for k, v in KEYS.items()) + """
  UVMOD_CONFIG     another config file path

Two things can answer inside the game; uvmod finds out which:
  UE4SS with the external Lua REPL (PR #1475; preview builds: github.com/glitchbunny0/RE-UE4SS/releases)
      with [Debug] ExternalLuaReplEnabled = 1 — runs code in any started Lua mod
  the VibeBridge mod, for official UE4SS builds — runs code in its own Lua state only
"""


def print_result(result, as_json: bool, timeout: float) -> int:
    if result.answer is None:
        print(timeout_message(result.delivery, timeout))
        return EXIT_NO_ANSWER
    if as_json:
        print(json.dumps(dict(result.answer, delivery=result.delivery), ensure_ascii=False))
    else:
        print(protocol.render_text(result.answer))
    return EXIT_OK if result.answer.get("ok") else EXIT_LUA_ERROR


def cmd_status(game: Game) -> int:
    s = game.status()
    if s.running is None:
        print("game: unknown (no *-Shipping.exe beside the ue4ss folder; set UVMOD_PROCESS)")
    elif s.running:
        print(f"game: running ({s.process}, pid {s.pid}, started {s.started or '?'})")
    else:
        print(f"game: not running ({s.process})")
    if s.log_time:
        print(f"log: {s.log_time}")
        for line in s.log_lines:
            print(f"  {line}")
    print(f"external REPL setting: {s.repl_setting or 'not in UE4SS-settings.ini (this UE4SS build has none)'}")
    if s.bridge_age is not None:
        state = "running" if s.bridge_age <= protocol.BRIDGE_FRESH_SECONDS else f"not running (last heartbeat {s.bridge_age:.0f} s ago)"
        print(f"VibeBridge: {state}")
    else:
        print(f"VibeBridge: {'installed, no heartbeat yet' if s.bridge_installed else 'not installed'}")
    hr = s.hot_reload_setting
    warn = "  <- unsafe once any mod hooks Blueprint functions (hang or crash on reload)" if hr.replace(" ", "").endswith("=1") else ""
    print(f"hot reload: {hr or '?'}{warn}")
    if not s.repl_folder:
        print("repl: no LuaRepl folder (see: uvmod help)")
        return EXIT_NO_ANSWER
    saved, game.settings.timeout = game.settings.timeout, min(game.settings.timeout, 3.0)
    try:
        listed = game.mods()
    finally:
        game.settings.timeout = saved
    if listed.answer and listed.answer.get("ok"):
        who = "VibeBridge" if listed.delivery == "bridge" else "UE4SS external REPL"
        print(f"repl: {who} answering in {listed.elapsed_s * 1000:.0f} ms; mods: {', '.join(listed.answer['mods'])}")
        mods = listed.answer["mods"]
        if game.settings.mod:
            target = game.settings.mod + ("" if game.settings.mod in mods else "  <- not among the mods above")
        elif len(mods) == 1:
            target = f"{mods[0]} (the only mod)"
        else:
            target = "none set: pass --mod or set UVMOD_MOD to one of the mods above"
        print(f"target: {target}")
        return EXIT_OK
    print(f"repl: not answering ({timeout_message(listed.delivery, 3.0)})")
    return EXIT_NO_ANSWER


def bridge_dir() -> str:
    return str(resources.files("uvmod") / "bridge" / "VibeBridge")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(HELP)
        return EXIT_OK
    if argv[0] in ("--version", "version"):
        print(__version__)
        return EXIT_OK

    parser = argparse.ArgumentParser(prog="uvmod", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
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
    for name in ("push", "swap"):
        sub.add_parser(name).add_argument("dir")
    p = sub.add_parser("shot")
    p.add_argument("out", nargs="?")
    p.add_argument("--mod")
    sub.add_parser("install-bridge")
    sub.add_parser("ps")
    sub.add_parser("mcp")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print("see: uvmod help", file=sys.stderr)
        return EXIT_USAGE

    if args.cmd == "mcp":
        from .mcp import serve

        return serve()

    try:
        game = Game(Settings.load())
        if args.cmd in ("lua", "run"):
            code = args.code if args.cmd == "lua" else (sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read())
            return print_result(game.exec(code, args.mod, args.limit), args.json, game.settings.timeout)
        if args.cmd == "mods":
            return print_result(game.mods(), args.json, game.settings.timeout)
        if args.cmd == "status":
            return cmd_status(game)
        if args.cmd == "log":
            print("\n".join(game.log(args.lines, args.pattern)))
            return EXIT_OK
        if args.cmd == "follow":
            try:
                for line in game.follow(args.pattern):
                    print(line, flush=True)
            except KeyboardInterrupt:
                pass
            return EXIT_OK
        if args.cmd == "push":
            print(game.push(args.dir))
            return EXIT_OK
        if args.cmd == "swap":
            ok, text = game.swap(args.dir)
            print(text)
            return EXIT_OK if ok else EXIT_LUA_ERROR
        if args.cmd == "shot":
            print(game.screenshot(args.out, args.mod))
            return EXIT_OK
        if args.cmd == "install-bridge":
            print(game.push(bridge_dir()))
            game.transport.make_folder(protocol.FOLDER)
            print(f"created {protocol.FOLDER}/. VibeBridge loads at the next game start; it stays idle if this UE4SS has its "
                  "external REPL turned on.")
            return EXIT_OK
        if args.cmd == "ps":
            print(game.transport.powershell(sys.stdin.read()), end="")
            return EXIT_OK
    except (ConfigError, GameError, TransportError, ValueError, OSError) as e:
        print(f"uvmod: {e}", file=sys.stderr)
        return EXIT_USAGE if isinstance(e, (ConfigError, ValueError)) else EXIT_NO_ANSWER
    print("see: uvmod help", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
