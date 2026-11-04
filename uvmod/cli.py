"""uvmod: drive a running Unreal Engine game through UE4SS. Run `uvmod help` for the commands."""

from __future__ import annotations

import argparse
import os
import sys
from importlib import resources

from . import __version__
from .config import CET_APP, UE4SS_APP, ConfigError, Settings
from .core import protocol
from .core.cli import (GENERIC_COMMANDS, EXIT_LUA_ERROR, EXIT_NO_ANSWER, EXIT_NOT_MET, EXIT_OK, EXIT_USAGE, add_generic,
                       run_generic)
from .core.config import keys_for
from .core.session import GameError
from .census import Census
from .game import UE_ONLY, Game
from .inputs import Input, parse_value, press_key
from .reflection import Inspector, parse_cli_args, render
from .core.transports import TransportError
from .watch import LuaError, Watcher, render_events, render_wait, render_watches


def _settings_help() -> list[tuple[str, str]]:
    """(KEY, help) pairs for the help text: UE4SS's root first, then CET's (as the list has always read), then the
    shared keys with the app's extra ones in their place (see keys_for)."""
    items = list(keys_for(UE4SS_APP).items())
    items.insert(1, next(iter(keys_for(CET_APP).items())))  # keys_for puts the app's root key first
    return items

HELP = f"""uvmod {__version__} — vibe modding for Unreal Engine games: run Lua live in a running game through UE4SS.

  uvmod lua 'return FindFirstOf("PlayerController"):GetFullName()'   run Lua on the game thread
  uvmod run file.lua | uvmod run -          ... from a file / stdin
  uvmod mods                                mods whose Lua state can run code
  uvmod status                              game running? which REPL answers? settings that matter
  uvmod log [N] [REGEX]                     last N lines of UE4SS.log (default 40)
  uvmod follow [REGEX]                      stream the log until Ctrl+C
  uvmod push DIR                            copy a mod folder into Mods/ (loads at the next game start)
  uvmod swap DIR                            push a hot-swap-shell mod and swap its logic in place
  uvmod shot [OUT.jpg] [--burst N [--every MS]] [--no-ui]   engine screenshot as the player sees it (HUD, menus, the
        overlay), copied here; --no-ui: the scene only. --burst: N shots MS apart (default 750), timed in the game and
        tiled into one numbered contact sheet, to judge motion. The engine writes one screenshot at a time (about
        0.7 s each at 1440p) and drops requests meanwhile, so shorter steps lose frames

  uvmod watch FUNCTION [--capture EXPR]     record each call of a UFunction, in the game, until unwatched
        e.g. uvmod watch /Script/Engine.PlayerController:ClientRestart
        --capture 'self.Health, select(1, ...)': what to record (Lua over self and the parameters; nil skips
        the call); default the object's name. --id NAME, --limit N (default 200),
        --post: record after the call; native functions only (UE4SS runs no post callbacks for Blueprint
        ones), and in a post capture `...` starts with the return value, if the function has one
  uvmod events                              what was recorded since the last events/wait
  uvmod wait [--for ID] [--count N]         block until N new calls were recorded (default 1, any watch) ...
  uvmod wait --until 'LUA EXPR'             ... or until the expression is true; --timeout S (default 30)
  uvmod watches | uvmod unwatch [ID]        list the watches / remove one (all without ID)

Reading the game (references: /Full/Path, first:Class, cdo:/Script/Pkg.Class, class:/Script/Pkg.Class, pc, pawn, world)
  uvmod world                               world, player controller, pawn and its location, game mode
  uvmod find REF | all:Class [--limit N]    objects: one reference, or every live instance of a class
  uvmod props REF [PATTERN] [--own]         properties with their types and values (--own: not inherited ones)
  uvmod funcs REF [PATTERN] [--own]         functions with their signatures and flags
  uvmod sig REF FUNCTION                    one function's parameters
  uvmod classes [PATTERN] [--base REF] [--kind class|struct|enum]   loaded types
  uvmod call REF FUNCTION [ARG ...]         call a UFunction behind a guard: refuses what would crash UE4SS (nil or a
        number for an FName, a non-table for a struct, an invalid object). ARGs are JSON where they parse
        ({{"X": 1}}, 2.5, true), else text; text becomes an FName, FText or object (a REF) where the parameter needs
        one. --lua 'EXPR, ...' passes Lua expressions instead.
  uvmod snap LABEL REF [PATTERN]            keep the object's property values under LABEL (in the game)
  uvmod diff LABEL [REF] [--update]         what changed since the snapshot (--update: make now the new baseline)
  uvmod snaps | uvmod forget [LABEL]        list the snapshots / drop one (all without LABEL)
  uvmod target [DISTANCE] [--from REF]      what the camera looks at (a line trace, default 5000 cm)

Playing it (Enhanced Input: the game's own mapping contexts, triggers and modifiers run as for real input)
  uvmod input list [PATTERN]                the loaded InputActions and their value types
  uvmod input press ACTION [--value X[,Y[,Z]]] [--hold MS]   inject a value for an InputAction (IA_Jump, or a path):
        once, or on every frame for MS milliseconds (a held key, a stick held over, the mouse moved)
  uvmod input stop | uvmod input status     release everything held / what is held
  uvmod input key KEY [--hold MS]           an OS key press in the game's window (brought to the front first), for a
        mod's UE4SS keybinds: F8, NUM_ONE, CONTROL+F8 (UE4SS Key names). Windows only (local, or over ssh)

Which functions run when something happens (the step before `watch`)
  uvmod calls discover REF ... [--idle S] [--act S] [--press ACTION [--value X,Y] [--hold MS]] [--native] [--pattern P]
        hook every Blueprint function of the objects' classes (--native: native ones too), count calls in an idle
        window and in an action window (pressing ACTION at its start, or telling you to act), and list what the
        action ran: functions new in it or at least 3x busier. The hooks are removed afterwards.
  uvmod calls start REF ... | mark | compare [--factor F] | stop     the same, one step at a time

The game's reflection, offline (an index built from UE4SS's own dump; no game needed once it is built)
  uvmod index [--from FILE.jmap] [--native-only] [--keep]   dump the running game's reflection (DumpJMAP) and index it
  uvmod api search TEXT [--kind class|struct|enum|function|property] [--limit N]
  uvmod api class NAME|PATH [--inherited]   properties (with defaults) and functions of a class, struct or enum
  uvmod api sig [Class:]Function            parameters with in / out / ref roles (what `call` needs)
  uvmod api subclasses NAME                 every class derived from it
  uvmod notes | uvmod notes add TEXT        this game's notes: what was learned (names that lie, limits, safe calls),
        kept for the next session; read them at the start of one
  uvmod check PATH ...                      read a mod's Lua against the index: a string or nil where a UFunction
        takes an FName is a native crash (error, exit 1); argument counts no function accepts (warning)

A new mod
  uvmod new NAME [--dir DIR]                a mod project: hot-swap shell, logic.lua, tests against a strict UE4SS
        stand-in (crashes like the game on an FName mistake), tests/run.sh, a README with the loop and a test plan
  uvmod mock [PROJECT]                      write PROJECT/tests/game_api.lua: the real signatures of the UFunctions the
        mod calls, from the reflection index, which the stand-in then enforces (default PROJECT: .)

The game itself
  uvmod launch | uvmod stop                 start the game (Steam, or UVMOD_LAUNCH) / end its process
  uvmod restart [--timeout S]               stop, launch, and wait until a REPL answers (load new mod builds)
  uvmod ready [--timeout S]                 wait until a REPL answers (default 180 s); exit 3 if it does not
  uvmod console 'CMD'                       an engine console command as the player: 'open /Game/Maps/X' loads a level
        (a known test scene). Shipping builds lack many commands (stat ...); cheats need the cheat manager
  uvmod crash                               why it died: crash folder, minidump stack, log tail, last requests sent
  uvmod journal [N]                         the last N requests uvmod sent and how each ended
  uvmod install-bridge                      install the VibeBridge mod (for UE4SS builds without the external REPL)
  uvmod ps < script.ps1                     run PowerShell on the game box (ssh transport)
  uvmod mcp                                 MCP server on stdio, for AI agents

Cyberpunk 2077 (Cyber Engine Tweaks: set UVMOD_CET instead of UVMOD_UE4SS)
  uvmod install-bridge                      install VibeBridge as a CET mod; then reload CET's mods (overlay: Reload all
        mods) or start the game. Lua runs in VibeBridge's environment (LuaJIT, CET's sandbox and API: Game, GetPlayer,
        TweakDB, Observe, ...); `vb.json(value)` gives JSON for any value. CET cannot stop a request: no endless loops
  uvmod cet [HANDLER [ARGS]]                CETBridge's game handlers: player_info, game_state, get_inventory,
        teleport, ... (no HANDLER: the list). ARGS: a JSON object or key=value pairs
  lua, run, mods, status, log (CET's scripting.log), follow, push (into mods/), shot (the game window, copied from
        the screen), launch, stop, restart, ready, crash (REDEngine's crash report), journal and mcp work too; the
        Unreal reflection, input, calls, watch, index and console commands do not

  lua/run/mods take --mod NAME, --limit MS (1-5000) and --json (print the raw answer); the watch commands
  take --mod too (watches live in that mod's Lua state).
  Exit codes: 0 OK, 1 the Lua raised an error, 2 usage or settings, 3 nothing answered, 4 wait timed out.

Settings come from the environment or from the config file (KEY=value lines; the environment wins):
""" + "\n".join(f"  {k:<16} {v}" for k, v in _settings_help()) + """
  UVMOD_CONFIG     another config file path

Two things can answer inside the game; uvmod finds out which:
  UE4SS with the external Lua REPL (PR #1475, not merged yet)
      with [Debug] ExternalLuaReplEnabled = 1 — runs code in any started Lua mod
  the VibeBridge mod, for official UE4SS builds — runs code in its own Lua state only
"""


def cmd_watch(watcher: Watcher, args) -> int:
    if args.cmd == "watch":
        batch = watcher.watch(args.function, args.id, args.capture, args.post, args.limit)
        print(render_watches(batch))
    elif args.cmd == "unwatch":
        batch = watcher.unwatch(args.id)
        print(f"watching: {', '.join(batch.active) or 'nothing'}")
    elif args.cmd == "watches":
        print(render_watches(watcher.list()))
    elif args.cmd == "events":
        print(render_events(watcher.events(args.since)))
    else:
        met, result, seconds = watcher.wait(args.for_id, args.count, args.until, args.timeout)
        print(render_wait(met, result, seconds))
        return EXIT_OK if met else EXIT_NOT_MET
    return EXIT_OK


def bridge_dir(engine: str = "ue4ss") -> str:
    if engine == "cet":
        return str(resources.files("uvmod") / "bridge" / "cet" / "VibeBridge")
    return str(resources.files("uvmod") / "bridge" / "VibeBridge")


REFLECTION_COMMANDS = ("world", "find", "props", "funcs", "sig", "classes", "call", "snap", "diff", "snaps", "forget", "target")
# What only UE4SS can do (Unreal reflection, Enhanced Input, Blueprint hooks, DumpJMAP, the engine console).
UE_ONLY_COMMANDS = REFLECTION_COMMANDS + ("watch", "unwatch", "events", "wait", "watches", "calls", "index", "console")


def cmd_reflection(insp: Inspector, args) -> int:
    c = args.cmd
    if c == "world":
        op, answer = "world", insp.world()
    elif c == "find":
        op, answer = "find", insp.find(args.spec, args.limit)
    elif c == "props":
        op, answer = "props", insp.props(args.ref, args.pattern, not args.own, args.limit)
    elif c == "funcs":
        op, answer = "functions", insp.functions(args.ref, args.pattern, not args.own, args.limit)
    elif c == "sig":
        op, answer = "signature", insp.signature(args.ref, args.function)
    elif c == "classes":
        op, answer = "classes", insp.classes(args.pattern, args.base, args.kind, args.limit)
    elif c == "call":
        if args.lua and args.args:
            raise ValueError("pass ARGs or --lua, not both")
        op, answer = "call", insp.call(args.ref, args.function, parse_cli_args(args.args), args.lua)
    elif c == "snap":
        op, answer = "snapshot", insp.snapshot(args.label, args.ref, args.pattern)
    elif c == "diff":
        op, answer = "diff", insp.diff(args.label, args.ref, args.update)
    elif c == "snaps":
        op, answer = "snapshots", insp.snapshots()
    elif c == "forget":
        op, answer = "forget", insp.forget(args.label)
    else:
        op, answer = "target", insp.target(args.distance, args.from_ref)
    print(render(op, answer))
    return EXIT_OK


def open_index():
    """The index `api` and `check` read; works without UVMOD_UE4SS (offline)."""
    from .index import Index, find

    try:
        settings = Settings.load()
        pinned, ue4ss = settings.index, settings.root
    except ConfigError:
        pinned, ue4ss = os.environ.get("UVMOD_INDEX", ""), ""
    path = find(pinned, ue4ss)
    if path is None:
        raise ConfigError("no reflection index yet: run `uvmod index` with the game running (or set UVMOD_INDEX)")
    return Index(path)


def api_answer(ix, action: str, what: str, kind: str = "", limit: int = 40, inherited: bool = False) -> str:
    if action == "info":
        return f"{ix.path}\n{ix.header()}"
    if not what:
        raise ValueError(f"api {action} needs a name")
    if action == "search":
        lines = ix.search(what, kind, limit)
    elif action == "class":
        lines = ix.describe(what, inherited)
    elif action == "sig":
        lines = ix.signature(what)
    else:
        lines = ix.subclasses(what, max(limit, 200))
    return "\n".join(lines + [ix.header()])


def cmd_offline(args) -> int:
    from . import scaffold
    from .lint import check_paths

    try:
        if args.cmd == "new":
            target = scaffold.new(args.name, args.dir)
            print(f"made {target}")
            try:
                ix = open_index()
                try:
                    out, written, unknown = scaffold.mock(str(target), ix)
                finally:
                    ix.close()
                print(f"wrote {out} ({written} functions from the reflection index)")
            except ConfigError:
                print("(no reflection index yet: run `uvmod index` with the game running, then `uvmod mock`)")
            print(f"next: {target}/tests/run.sh, then uvmod push {target / args.name} and uvmod restart")
            return EXIT_OK
        ix = open_index()
        try:
            return offline_with(ix, args, scaffold, check_paths)
        finally:
            ix.close()
    except (ConfigError, ValueError, OSError) as e:
        print(f"uvmod: {e}", file=sys.stderr)
        return EXIT_USAGE


def offline_with(ix, args, scaffold, check_paths) -> int:
    if args.cmd == "mock":
        out, written, unknown = scaffold.mock(args.project, ix)
        print(f"wrote {out}: {written} functions" + (f"; not in the index (Lua or UE4SS functions?): {', '.join(unknown)}" if unknown else ""))
        return EXIT_OK
    if args.cmd == "api":
        print(api_answer(ix, args.action, args.what, args.kind, args.limit, args.inherited))
        return EXIT_OK
    findings = check_paths(ix, args.paths)
    for f in findings:
        print(f)
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = len(findings) - errors
    print(f"{errors} error{'s' if errors != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''} {ix.header()}")
    return EXIT_LUA_ERROR if errors else EXIT_OK


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
    add_generic(sub)
    p = sub.add_parser("shot")
    p.add_argument("out", nargs="?")
    p.add_argument("--mod")
    p.add_argument("--burst", type=int, default=0)
    p.add_argument("--every", type=int, default=750)
    p.add_argument("--no-ui", action="store_true")
    p = sub.add_parser("watch")
    p.add_argument("function")
    p.add_argument("--id")
    p.add_argument("--capture")
    p.add_argument("--post", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("unwatch")
    p.add_argument("id", nargs="?")
    p = sub.add_parser("events")
    p.add_argument("--since", type=int)
    p = sub.add_parser("wait")
    p.add_argument("--for", dest="for_id")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--until")
    p.add_argument("--timeout", type=float, default=30.0)
    sub.add_parser("watches")
    for name in ("watch", "unwatch", "events", "wait", "watches"):
        sub.choices[name].add_argument("--mod")
    sub.add_parser("world")
    p = sub.add_parser("find")
    p.add_argument("spec")
    p.add_argument("--limit", type=int, default=50)
    for name in ("props", "funcs"):
        p = sub.add_parser(name)
        p.add_argument("ref")
        p.add_argument("pattern", nargs="?")
        p.add_argument("--own", action="store_true")
        p.add_argument("--limit", type=int, default=400 if name == "props" else 300)
    p = sub.add_parser("sig")
    p.add_argument("ref")
    p.add_argument("function")
    p = sub.add_parser("classes")
    p.add_argument("pattern", nargs="?")
    p.add_argument("--base")
    p.add_argument("--kind", default="class", choices=("class", "struct", "enum"))
    p.add_argument("--limit", type=int, default=200)
    p = sub.add_parser("call")
    p.add_argument("ref")
    p.add_argument("function")
    p.add_argument("args", nargs="*")
    p.add_argument("--lua")
    p = sub.add_parser("snap")
    p.add_argument("label")
    p.add_argument("ref")
    p.add_argument("pattern", nargs="?")
    p = sub.add_parser("diff")
    p.add_argument("label")
    p.add_argument("ref", nargs="?")
    p.add_argument("--update", action="store_true")
    sub.add_parser("snaps")
    p = sub.add_parser("forget")
    p.add_argument("label", nargs="?")
    p = sub.add_parser("target")
    p.add_argument("distance", nargs="?", type=float, default=5000.0)
    p.add_argument("--from", dest="from_ref")
    for name in REFLECTION_COMMANDS:
        sub.choices[name].add_argument("--mod")
    p = sub.add_parser("input")
    p.add_argument("action", choices=("list", "press", "stop", "status", "key"))
    p.add_argument("what", nargs="?")
    p.add_argument("--value")
    p.add_argument("--hold", type=int, default=0)
    p.add_argument("--mod")
    p = sub.add_parser("calls")
    p.add_argument("action", choices=("discover", "start", "mark", "compare", "stop"))
    p.add_argument("refs", nargs="*")
    p.add_argument("--idle", type=float, default=3.0)
    p.add_argument("--act", type=float, default=5.0)
    p.add_argument("--press")
    p.add_argument("--value")
    p.add_argument("--hold", type=int, default=0)
    p.add_argument("--native", action="store_true")
    p.add_argument("--pattern")
    p.add_argument("--factor", type=float, default=3.0)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--mod")
    p = sub.add_parser("index")
    p.add_argument("--from", dest="from_file")
    p.add_argument("--native-only", action="store_true")
    p.add_argument("--keep", action="store_true")
    p = sub.add_parser("api")
    p.add_argument("action", choices=("search", "class", "sig", "subclasses", "info"))
    p.add_argument("what", nargs="?", default="")
    p.add_argument("--kind", default="", choices=("", "class", "struct", "enum", "function", "property"))
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--inherited", action="store_true")
    sub.add_parser("check").add_argument("paths", nargs="+")
    p = sub.add_parser("notes")
    p.add_argument("action", nargs="?", default="show", choices=("show", "add"))
    p.add_argument("text", nargs="*")
    p = sub.add_parser("new")
    p.add_argument("name")
    p.add_argument("--dir", default=".")
    sub.add_parser("mock").add_argument("project", nargs="?", default=".")
    sub.add_parser("swap").add_argument("dir")
    p = sub.add_parser("console")
    p.add_argument("command")
    p.add_argument("--mod")
    sub.add_parser("install-bridge")
    p = sub.add_parser("cet")
    p.add_argument("handler", nargs="?")
    p.add_argument("args", nargs="*")
    p.add_argument("--mod")
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
    if args.cmd in ("api", "check", "new", "mock"):
        return cmd_offline(args)

    try:
        game = Game(Settings.load())
        if game.settings.is_cet and (args.cmd in UE_ONLY_COMMANDS or (args.cmd == "input" and args.action != "key")):
            print(f"uvmod: {args.cmd}: {UE_ONLY}", file=sys.stderr)
            return EXIT_USAGE
        if args.cmd == "cet":
            from . import cet

            if not args.handler:
                print(cet.catalog())
                return EXIT_OK
            if not game.settings.is_cet:
                raise GameError("CETBridge's handlers need Cyberpunk 2077 with CET (set UVMOD_CET)")
            answer = game.query(args.handler, cet.parse_args(" ".join(args.args)), args.mod)
            print(cet.render(answer))
            return EXIT_OK if answer.get("ok") else EXIT_LUA_ERROR
        if args.cmd in GENERIC_COMMANDS:
            return run_generic(game, args)
        if args.cmd == "swap":
            ok, text = game.swap(args.dir)
            print(text)
            return EXIT_OK if ok else EXIT_LUA_ERROR
        if args.cmd == "shot":
            if args.burst:
                path = game.burst(args.burst, args.every, args.out, args.mod, ui=not args.no_ui)
                got, asked = game.last_burst
                print(f"{path} ({got} of {asked} frames" + ("; the engine drops shots requested while it still writes one" if got < asked else "") + ")")
            else:
                print(game.screenshot(args.out, args.mod, ui=not args.no_ui))
            return EXIT_OK
        if args.cmd in ("watch", "unwatch", "events", "wait", "watches"):
            return cmd_watch(Watcher(game, args.mod), args)
        if args.cmd in REFLECTION_COMMANDS:
            return cmd_reflection(Inspector(game, args.mod), args)
        if args.cmd == "input":
            inp = Input(game, args.mod)
            if args.action == "list":
                print(inp.list(args.what))
            elif args.action == "press":
                if not args.what:
                    raise ValueError("input press needs an InputAction")
                print(inp.press(args.what, parse_value(args.value), args.hold))
            elif args.action == "key":
                if not args.what:
                    raise ValueError("input key needs a key name (F8, NUM_ONE, CONTROL+F8)")
                print(press_key(game, args.what, args.hold or 150))
            elif args.action == "stop":
                print(inp.stop())
            else:
                print(inp.status())
            return EXIT_OK
        if args.cmd == "calls":
            census = Census(game, args.mod)
            if args.action == "discover":
                print(census.discover(args.refs, args.idle, args.act, args.press, parse_value(args.value), args.hold,
                                      args.native, args.pattern,
                                      prompt=lambda msg: print(f">>> {msg}", flush=True)))
            elif args.action == "start":
                print(census.start(args.refs, args.native, args.pattern))
            elif args.action == "mark":
                print(census.mark(args.limit))
            elif args.action == "compare":
                print(census.compare(args.factor, args.limit))
            else:
                print(census.stop())
            return EXIT_OK
        if args.cmd == "index":
            print(game.build_index(args.from_file, not args.native_only, args.keep))
            return EXIT_OK
        if args.cmd == "notes":
            from . import notes

            print(notes.add(game.settings.root, " ".join(args.text)) if args.action == "add" else notes.read(game.settings.root))
            return EXIT_OK
        if args.cmd == "console":
            print(game.console(args.command, args.mod))
            return EXIT_OK
        if args.cmd == "install-bridge":
            engine = game.transport.engine
            print(game.push(bridge_dir(engine.name)))
            game.transport.make_folder(engine.repl)
            if game.settings.is_cet:
                print(f"created {engine.repl}/. VibeBridge loads when CET loads its mods: reload them in CET's overlay "
                      "(Reload all mods) or start the game. DEVELOPMENT ONLY: whatever can write to that folder runs Lua "
                      "in the game.")
            else:
                print(f"created {protocol.FOLDER}/. VibeBridge loads at the next game start; it stays idle if this UE4SS has its "
                      "external REPL turned on.")
            return EXIT_OK
        if args.cmd == "ps":
            print(game.transport.powershell(sys.stdin.read()), end="")
            return EXIT_OK
    except LuaError as e:
        print(f"ERROR: {e}")
        return EXIT_LUA_ERROR
    except (ConfigError, GameError, TransportError, ValueError, OSError) as e:
        print(f"uvmod: {e}", file=sys.stderr)
        return EXIT_USAGE if isinstance(e, (ConfigError, ValueError)) else EXIT_NO_ANSWER
    print("see: uvmod help", file=sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
