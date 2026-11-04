"""cetmod: drive a running Cyberpunk 2077 through Cyber Engine Tweaks. Run `cetmod help` for the commands."""

from __future__ import annotations

import argparse
import sys
from importlib import resources

from . import APP, __version__
from .config import Settings
from .game import Game
from .handlers import catalog, parse_args, render
from uvmod.core.cli import (GENERIC_COMMANDS, EXIT_LUA_ERROR, EXIT_NO_ANSWER, EXIT_OK, EXIT_USAGE, add_generic,
                            run_generic)
from uvmod.core.config import ConfigError, keys_for
from uvmod.core.session import GameError
from uvmod.core.transports import TransportError

HELP = f"""cetmod {__version__} — vibe modding for Cyberpunk 2077: run Lua live in the running game through Cyber Engine
Tweaks (CET, with the VibeBridge CET mod).

  cetmod lua 'return Game.GetPlayer():GetDisplayName()'   run Lua in the game (LuaJIT, in VibeBridge's environment)
  cetmod run file.lua | cetmod run -        ... from a file / stdin
  cetmod mods                              mods whose Lua state can run code
  cetmod status                            game running? does VibeBridge answer? settings that matter
  cetmod log [N] [REGEX]                   last N lines of CET's scripting.log (default 40)
  cetmod follow [REGEX]                    stream the log until Ctrl+C
  cetmod push DIR                          copy a CET mod folder (with init.lua) into mods/ (loads when CET reloads
        its mods: overlay, Reload all mods — or at the next game start)
  cetmod shot [OUT.jpg]                    the game's window copied from the screen as the player sees it (HUD and
        menus included; only while the game is the window in front)
  cetmod query [HANDLER [ARGS]]            CETBridge's game handlers: player_info, game_state, get_inventory,
        teleport, ... (no HANDLER: the list). ARGS: a JSON object or key=value pairs
  cetmod install-bridge                    install VibeBridge as a CET mod; then reload CET's mods (overlay: Reload all
        mods) or start the game. Lua runs in VibeBridge's environment (LuaJIT, CET's sandbox and API: Game, GetPlayer,
        TweakDB, Observe, ...); `vb.json(value)` gives JSON for any value. CET cannot stop a request: no endless loops

The game itself
  cetmod launch | cetmod stop              start the game (its exe, or CETMOD_LAUNCH) / end its process
  cetmod restart [--timeout S]             stop, launch, and wait until VibeBridge answers (load new mod builds)
  cetmod ready [--timeout S]               wait until VibeBridge answers (default 180 s); exit 3 if it does not
  cetmod crash                             why it died: REDEngine's crash report, log tail, last requests sent
  cetmod journal [N]                       the last N requests cetmod sent and how each ended
  cetmod mcp                               MCP server on stdio, for AI agents

  lua/run/mods take --mod NAME, --limit MS (1-5000) and --json (print the raw answer).
  Exit codes: 0 OK, 1 the Lua raised an error, 2 usage or settings, 3 nothing answered, 4 wait timed out.

Settings come from the environment or from the config file (KEY=value lines; the environment wins):
""" + "\n".join(f"  {k:<16} {v}" for k, v in keys_for(APP).items()) + """
  CETMOD_CONFIG    another config file path

VibeBridge answers inside the game; `status` says whether it does.
"""


def bridge_dir() -> str:
    return str(resources.files("cetmod") / "bridge" / "VibeBridge")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(HELP)
        return EXIT_OK
    if argv[0] in ("--version", "version"):
        print(__version__)
        return EXIT_OK

    parser = argparse.ArgumentParser(prog="cetmod", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    add_generic(sub)
    p = sub.add_parser("shot")
    p.add_argument("out", nargs="?")
    p = sub.add_parser("query")
    p.add_argument("handler", nargs="?")
    p.add_argument("args", nargs="*")
    p.add_argument("--mod")
    sub.add_parser("install-bridge")
    sub.add_parser("mcp")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print("see: cetmod help", file=sys.stderr)
        return EXIT_USAGE

    if args.cmd == "mcp":
        from .mcp import serve

        return serve()
    try:
        game = Game(Settings.load())
        if args.cmd == "query":
            if not args.handler:
                print(catalog())
                return EXIT_OK
            answer = game.query(args.handler, parse_args(" ".join(args.args)), args.mod)
            print(render(answer))
            return EXIT_OK if answer.get("ok") else EXIT_LUA_ERROR
        if args.cmd in GENERIC_COMMANDS:
            return run_generic(game, args)
        if args.cmd == "shot":
            print(game.screenshot(args.out))
            return EXIT_OK
        if args.cmd == "install-bridge":
            engine = game.transport.engine
            print(game.push(bridge_dir()))
            game.transport.make_folder(engine.repl)
            print(f"created {engine.repl}/. VibeBridge loads when CET loads its mods: reload them in CET's overlay "
                  "(Reload all mods) or start the game. DEVELOPMENT ONLY: whatever can write to that folder runs Lua "
                  "in the game.")
            return EXIT_OK
    except (ConfigError, GameError, TransportError, ValueError, OSError) as e:
        print(f"cetmod: {e}", file=sys.stderr)
        return EXIT_USAGE if isinstance(e, (ConfigError, ValueError)) else EXIT_NO_ANSWER
    print("see: cetmod help", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
