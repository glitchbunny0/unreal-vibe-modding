"""MCP server on stdio (newline-delimited JSON-RPC 2.0) over the same core as the CLI. Stdlib only."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
from typing import Any

from . import __version__, protocol
from .config import ConfigError, Settings
from .game import Game, GameError, timeout_message
from .census import Census
from .inputs import Input
from .reflection import Inspector, render
from .transports import TransportError
from .watch import LuaError, Watcher, render_events, render_wait, render_watches

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """Drives a running Unreal Engine game through UE4SS: run Lua on the game thread, read the log, take screenshots,
push mods. Start with `status`. Code runs with the full power of a mod, inside the player's game: a mistake can crash
or freeze it, so read before you write, keep changes small, and look at the result (screenshot, log) after each one."""

LUA_RULES = """Runs Lua on the game thread inside a UE4SS Lua mod's state, and returns printed lines, return values (display
strings) or the error with a traceback. Globals persist between calls; locals do not.
Rules that crash the game when broken (a native crash, not a Lua error):
- A UFunction parameter of type FName needs FName("Text"): a Lua string or nil there crashes UE4SS.
- Never use a UObject after the game may have destroyed it; check obj:IsValid() first.
- FindFirstOf/FindAllOf return invalid objects rather than nil; test IsValid().
Also: a property's name is a guess about its meaning, so measure what it does before relying on it (`watch` helps). Keep loops
bounded: the request stops after limit_ms (default 250, at most 5000), but a single long native call is not
interrupted. RegisterHook callbacks run later on the game thread; register them once and remember what you added."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "lua",
        "description": LUA_RULES,
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Lua source; tried as `return <code>` first, then as statements."},
                "mod": {"type": "string", "description": "Mod whose Lua state runs the code (see `mods`). Default: UVMOD_MOD or the only mod."},
                "limit_ms": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Time limit; default 250."},
            },
            "required": ["code"],
        },
    },
    {"name": "mods", "description": "Lists the mods whose Lua state `lua` can run code in.", "inputSchema": {"type": "object", "properties": {}}},
    {
        "name": "status",
        "description": "Is the game running, which REPL answers (UE4SS's own or the VibeBridge mod), the settings that matter.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "log",
        "description": "The last lines of UE4SS.log, optionally only those matching a regular expression (case-insensitive).",
        "inputSchema": {
            "type": "object",
            "properties": {"lines": {"type": "integer", "minimum": 1, "maximum": 2000}, "pattern": {"type": "string"}},
        },
    },
    {
        "name": "screenshot",
        "description": ("Takes an engine screenshot of the game as the player sees it and returns the image. Look at the game "
                        "yourself instead of asking the player to describe it. With `burst` (2-16), takes that many shots "
                        "`every_ms` apart (default 750, timed inside the game) and returns them tiled into one numbered contact "
                        "sheet: for motion and animation, which one frame cannot show. The engine writes one screenshot at a "
                        "time (about 0.7 s each at 1440p) and drops requests meanwhile, so shorter steps return fewer frames."),
        "inputSchema": {"type": "object", "properties": {"mod": {"type": "string"},
                                                         "burst": {"type": "integer", "minimum": 2, "maximum": 16},
                                                         "every_ms": {"type": "integer", "minimum": 50, "maximum": 5000}}},
    },
    {
        "name": "watch",
        "description": (
            "Records each call of a UFunction (native or Blueprint) into a buffer inside the game, for `events` and "
            "`wait`. Use it to learn what the game does when the player acts: watch, tell the player what to do, then "
            "`wait` or `events`. Cheaper and more reliable than printing from hooks and reading the log. "
            "`function` is the full path, e.g. /Script/Engine.PlayerController:ClientRestart or "
            "/Game/Path/BP_Thing.BP_Thing_C:DoThing (a Blueprint class must be loaded first). `capture` is a Lua "
            "expression over `self` and the function's parameters `...` (already read with :get()), e.g. "
            "`self.Health, select(1, ...)`; its value is recorded, and nil skips the call. Without it, the object's "
            "name is recorded. Records at most `limit` calls (default 200), then only counts them. The same id again "
            "replaces the watch. The capture runs inside the game on every call with no time limit: keep it short."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string"},
                "id": {"type": "string", "description": "Name for the watch; default the function's name."},
                "capture": {"type": "string"},
                "post": {"type": "boolean", "description": "Record after the call instead of before it. Native functions only: UE4SS runs no post callbacks for Blueprint functions. In a post capture, `...` starts with the return value (if the function has one), then the parameters."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100000},
                "mod": {"type": "string", "description": "Mod whose Lua state holds the watch; use the same one for events and wait."},
            },
            "required": ["function"],
        },
    },
    {
        "name": "events",
        "description": "What the watches recorded since the last `events` or `wait`. Identical consecutive calls are collapsed into one line.",
        "inputSchema": {"type": "object", "properties": {"mod": {"type": "string"}}},
    },
    {
        "name": "wait",
        "description": (
            "Blocks until `count` new calls of watch `for` were recorded (any watch if omitted), or, with `until`, "
            "until that Lua expression is true; at most `timeout_s` seconds (default 30, max 120). Tell the player "
            "what to do before calling it. Returns the events it saw, or the expression's value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "for": {"type": "string"},
                "count": {"type": "integer", "minimum": 1},
                "until": {"type": "string"},
                "timeout_s": {"type": "number", "minimum": 0, "maximum": 120},
                "mod": {"type": "string"},
            },
        },
    },
    {
        "name": "unwatch",
        "description": "Removes a watch, or every watch without `id`. Watches also end when their mod restarts.",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "mod": {"type": "string"}}},
    },
    {
        "name": "push",
        "description": "Copies a mod folder from this machine into the game's Mods/ folder. It loads at the next game start (UE4SS hot reload is unsafe once mods hook Blueprint functions).",
        "inputSchema": {"type": "object", "properties": {"dir": {"type": "string"}}, "required": ["dir"]},
    },
    {
        "name": "swap",
        "description": "For a mod built as a hot-swap shell: pushes it and swaps its logic in the running game without a restart. Fails if the running mod is not a shell yet.",
        "inputSchema": {"type": "object", "properties": {"dir": {"type": "string"}}, "required": ["dir"]},
    },
    {
        "name": "game",
        "description": (
            "Starts and stops the game, and finds out why it died. `launch` starts it (Steam, or the UVMOD_LAUNCH "
            "command), `stop` ends its process, `restart` does both and waits until a REPL answers (that is how new mod "
            "builds load), `ready` waits for a REPL (up to `timeout_s`), `crash` reports why it died: the newest crash "
            "folder (error, call stack, the faulting module and offset from the minidump), the end of UE4SS.log, and the "
            "last requests sent from here (the one without an answer is the first suspect). After a crash, read `crash` "
            "before restarting, and do not repeat the call that killed it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["launch", "stop", "restart", "ready", "crash"]},
                "timeout_s": {"type": "number", "minimum": 10, "maximum": 600},
            },
            "required": ["action"],
        },
    },
    {
        "name": "api",
        "description": (
            "Answers from the reflection index, offline, without touching the game: built from the game's own reflection "
            "dump (`uvmod index`, once per game build). `search` finds classes, structs, enums, functions and properties "
            "whose name contains `text` (ranked: exact, prefix, contains); `class` lists a class's (or struct's, enum's) "
            "properties with their defaults and its functions with signatures (`inherited` adds the parents'); `sig` gives "
            "a function's parameters with their roles (in, out: pass {} and read it after, ref, return) as `Class:Function` "
            "or a unique name; `subclasses`; `check` reads a mod's Lua files at `path` (on this machine) and reports a "
            "string or nil passed where a UFunction takes an FName (a native crash) and impossible argument counts. "
            "Prefer it over live inspection for finding names: it covers everything the game had loaded when it was "
            "dumped, costs nothing, and cannot crash anything. `notes` reads this game's notes (read them first in a "
            "session); `note` appends `text`: write down what you measured (a name that lies, a clamp limit, a call "
            "that crashes, how to reach a test scene) so the next session does not have to find it again."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["search", "class", "sig", "subclasses", "check", "info", "notes", "note"]},
                "text": {"type": "string", "description": "search: part of a name; class/sig/subclasses: a name or path"},
                "kind": {"type": "string", "enum": ["class", "struct", "enum", "function", "property"]},
                "inherited": {"type": "boolean"},
                "path": {"type": "string", "description": "check: a mod folder or .lua file on this machine"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            "required": ["action"],
        },
    },
    {
        "name": "input",
        "description": (
            "Plays the game through its own Enhanced Input, so you can test controls yourself instead of asking the "
            "player. `list` shows the loaded InputActions (IA_Jump, IA_Move, IA_Look, ...) and their value types; `press` "
            "injects `value` for `name` once, or on every frame for `hold_ms` (a held button, a stick held over, the mouse "
            "moved for a while: IA_Look with value [x, y]); `stop` releases everything held. The game's mapping contexts, "
            "triggers and modifiers run as for real input, so an action only does something where the game would react "
            "to it. Verify the effect (watch the InpActEvt_<Action> functions, snapshot, screenshot). Say so before "
            "moving the player's character while they play."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "press", "stop", "status"]},
                "name": {"type": "string", "description": "press: an InputAction name (IA_Jump) or path; list: a filter"},
                "value": {"type": "array", "items": {"type": "number"}, "minItems": 1, "maxItems": 3,
                          "description": "X (button: 1), X,Y (2D axis), X,Y,Z; default [1]"},
                "hold_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                "mod": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "calls",
        "description": (
            "Finds out which functions run when something happens, before you know what to `watch`. `discover` hooks "
            "every Blueprint function of the classes of `refs` (pawn, pc, first:BP_Door_C, ...; `native` adds native "
            "functions), counts calls for `idle_s` seconds, then for `act_s` seconds during which `press` (an "
            "InputAction) is injected, or the player acts (tell them first), and lists the functions that were new in "
            "the action window or at least 3x busier; the hooks are removed afterwards. `start`/`mark`/`compare`/`stop` "
            "do the same in steps (mark closes a window). Counting runs a little Lua per call inside the game."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["discover", "start", "mark", "compare", "stop"]},
                "refs": {"type": "array", "items": {"type": "string"}},
                "idle_s": {"type": "number", "minimum": 0.5, "maximum": 60},
                "act_s": {"type": "number", "minimum": 0.5, "maximum": 110},
                "press": {"type": "string"},
                "value": {"type": "array", "items": {"type": "number"}, "minItems": 1, "maxItems": 3},
                "hold_ms": {"type": "integer", "minimum": 0, "maximum": 60000},
                "native": {"type": "boolean"},
                "pattern": {"type": "string"},
                "mod": {"type": "string"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "inspect",
        "description": (
            "Reads the game's reflection without changing anything. Start an exploration here, before writing Lua. "
            "`what`: world (world, player controller, pawn, game mode), find (`ref`, or all:ShortClassName for every live "
            "instance), props (properties of `ref` with types and current values), functions (functions of `ref` with "
            "signatures and flags), signature (`name` on `ref`), classes (loaded classes/structs/enums whose name contains "
            "`pattern`, optionally subclasses of `base`), target (what the camera looks at, a line trace over `distance` cm). "
            "References: /Full/Object.Path, first:ShortClassName, cdo:/Script/Pkg.Class, class:/Script/Pkg.Class, pc, pawn, "
            "world. `pattern` filters by a case-insensitive substring of the name. A property's name is a guess about its "
            "meaning: measure it (snapshot, watch) before relying on it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "what": {"type": "string", "enum": ["world", "find", "props", "functions", "signature", "classes", "target"]},
                "ref": {"type": "string"},
                "name": {"type": "string", "description": "signature: the function's name"},
                "pattern": {"type": "string"},
                "own": {"type": "boolean", "description": "props/functions: only those declared on the object's own class"},
                "base": {"type": "string", "description": "classes: only subclasses of this class (path or reference)"},
                "kind": {"type": "string", "enum": ["class", "struct", "enum"]},
                "distance": {"type": "number"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                "mod": {"type": "string"},
            },
            "required": ["what"],
        },
    },
    {
        "name": "call",
        "description": (
            "Calls a UFunction on an object behind a guard, and returns its return value. The guard reads the function's "
            "parameters first and REFUSES what would crash UE4SS natively instead of raising a Lua error: nil or a number for "
            "an FName parameter, a non-table for a struct, an invalid object. `args` is a JSON array, one per parameter (no "
            "return value): text becomes an FName, an FText, or the object a reference names (first:Class, /Path, pawn) "
            "where the parameter needs one; objects become tables ({\"X\": 1, \"Y\": 0, \"Z\": 0} for a vector); pass {} for "
            "an out parameter and read it in the answer. `lua_args` passes Lua expressions instead (e.g. "
            "'FName(\"Hand\"), {X = 1}'). `function` may be a full path (/Script/Pkg.Interface:Func) to call an interface's function with `ref` as its context. Use inspect signature first. A call changes the game: calling the game's own "
            "function keeps its logic and replication, unlike writing a property."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "function": {"type": "string"},
                "args": {"type": "array"},
                "lua_args": {"type": "string"},
                "limit_ms": {"type": "integer", "minimum": 1, "maximum": 5000},
                "mod": {"type": "string"},
            },
            "required": ["ref", "function"],
        },
    },
    {
        "name": "snapshot",
        "description": (
            "Finds out what an action changes: `take` keeps the readable property values of `ref` under `label` inside "
            "the game; tell the player what to do (or call a function); `diff` lists what changed since (with `update`, "
            "now becomes the new baseline). `list` shows the snapshots, `forget` drops one (all without label). "
            "`pattern` limits the properties taken."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["take", "diff", "list", "forget"]},
                "label": {"type": "string"},
                "ref": {"type": "string"},
                "pattern": {"type": "string"},
                "update": {"type": "boolean"},
                "mod": {"type": "string"},
            },
            "required": ["action"],
        },
    },
]


def text(content: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": content}], "isError": is_error}


class Server:
    def __init__(self):
        self.game: Game | None = None

    def _game(self) -> Game:
        if self.game is None:
            self.game = Game(Settings.load())
        return self.game

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "api" and args.get("action") in ("notes", "note"):
            from . import notes

            settings = Settings.load()
            if args.get("action") == "note":
                return text(notes.add(settings.ue4ss, str(args.get("text") or "")))
            return text(notes.read(settings.ue4ss))
        if name == "api":
            from .cli import api_answer, open_index
            from .lint import check_paths

            ix = open_index()
            try:
                action = str(args.get("action") or "")
                if action == "check":
                    findings = check_paths(ix, [str(args.get("path") or "")])
                    errors = sum(1 for f in findings if f.severity == "error")
                    body = "\n".join(str(f) for f in findings) or "no findings"
                    return text(f"{body}\n{errors} errors, {len(findings) - errors} warnings {ix.header()}", errors > 0)
                return text(api_answer(ix, action, str(args.get("text") or ""), str(args.get("kind") or ""),
                                       int(args.get("limit") or 40), bool(args.get("inherited"))))
            finally:
                ix.close()
        game = self._game()
        if name in ("lua", "mods"):
            if name == "lua":
                result = game.exec(str(args.get("code", "")), args.get("mod") or None, args.get("limit_ms"))
            else:
                result = game.mods()
            if result.answer is None:
                return text(timeout_message(result.delivery, game.settings.timeout), True)
            via = "VibeBridge" if result.delivery == "bridge" else "UE4SS"
            in_game = f"{result.answer['elapsed_ms']:.2f} ms in the game, " if "elapsed_ms" in result.answer else ""
            footer = f"\n({via}, {in_game}{result.elapsed_s * 1000:.0f} ms round trip)"
            return text(protocol.render_text(result.answer) + footer, not result.answer.get("ok"))
        if name == "status":
            from io import StringIO
            from contextlib import redirect_stdout
            from .cli import cmd_status

            buf = StringIO()
            with redirect_stdout(buf):
                rc = cmd_status(game)
            return text(buf.getvalue().rstrip(), rc != 0)
        if name == "log":
            lines = game.log(int(args.get("lines") or 40), str(args.get("pattern") or ""))
            return text("\n".join(lines) or "(no matching lines)")
        if name == "screenshot":
            if args.get("burst"):
                path = game.burst(int(args["burst"]), int(args.get("every_ms") or 750), mod=args.get("mod") or None)
                if os.path.isdir(path):
                    return text(f"no ffmpeg here to tile them; the frames are in {path}")
                got, asked = game.last_burst
                note = f"{got} of {asked} frames, numbered in order"
            else:
                path = game.screenshot(mod=args.get("mod") or None)
                note = ""
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            mime = "image/png" if path.endswith(".png") else "image/jpeg"
            caption = f"saved: {path}" + (f" ({note})" if note else "")
            return {"content": [{"type": "image", "data": data, "mimeType": mime}, {"type": "text", "text": caption}], "isError": False}
        if name in ("watch", "events", "wait", "unwatch"):
            watcher = Watcher(game, args.get("mod") or None)
            if name == "watch":
                batch = watcher.watch(str(args["function"]), args.get("id") or None, args.get("capture") or None,
                                      bool(args.get("post")), int(args.get("limit") or 200))
                return text(render_watches(batch) + f"\nwatching: {', '.join(batch.active)}")
            if name == "events":
                return text(render_events(watcher.events()))
            if name == "unwatch":
                batch = watcher.unwatch(args.get("id") or None)
                return text(f"watching: {', '.join(batch.active) or 'nothing'}")
            timeout = min(float(args.get("timeout_s", 30) or 0), 120.0)
            met, result, seconds = watcher.wait(args.get("for") or None, int(args.get("count") or 1),
                                                args.get("until") or None, timeout)
            return text(render_wait(met, result, seconds))
        if name == "push":
            return text(game.push(str(args["dir"])))
        if name == "swap":
            ok, answer = game.swap(str(args["dir"]))
            return text(answer, not ok)
        if name == "game":
            action = args.get("action")
            timeout = float(args.get("timeout_s") or 180)
            if action == "launch":
                return text(game.launch())
            if action == "stop":
                return text(game.stop())
            if action == "crash":
                return text(game.crash_report())
            if action in ("ready", "restart"):
                lines = []
                if action == "restart":
                    lines.append(game.stop())
                    time.sleep(2.0)
                    lines.append(game.launch())
                    time.sleep(10.0)
                ok, answer = game.ready(timeout)
                return text("\n".join(lines + [answer]), not ok)
            raise ValueError("action is launch, stop, restart, ready or crash")
        if name == "calls":
            census = Census(game, args.get("mod") or None)
            action = args.get("action")
            refs = [str(r) for r in (args.get("refs") or [])]
            if action == "discover":
                value = [float(v) for v in (args.get("value") or [1.0])][:3] + [0.0, 0.0]
                return text(census.discover(refs, float(args.get("idle_s") or 3), float(args.get("act_s") or 5),
                                            args.get("press") or None, (value[0], value[1], value[2]),
                                            int(args.get("hold_ms") or 0), bool(args.get("native")), args.get("pattern") or None))
            if action == "start":
                return text(census.start(refs, bool(args.get("native")), args.get("pattern") or None))
            if action == "mark":
                return text(census.mark())
            if action == "compare":
                return text(census.compare())
            if action == "stop":
                return text(census.stop())
            raise ValueError("action is discover, start, mark, compare or stop")
        if name == "input":
            inp = Input(game, args.get("mod") or None)
            action = args.get("action")
            if action == "list":
                return text(inp.list(args.get("name") or None))
            if action == "press":
                value = [float(v) for v in (args.get("value") or [1.0])][:3]
                value += [0.0] * (3 - len(value))
                return text(inp.press(str(args.get("name") or ""), (value[0], value[1], value[2]), int(args.get("hold_ms") or 0)))
            if action == "stop":
                return text(inp.stop())
            if action == "status":
                return text(inp.status())
            raise ValueError("action is list, press, stop or status")
        if name in ("inspect", "call", "snapshot"):
            return text(self._reflection(game, name, args))
        raise KeyError(name)

    @staticmethod
    def _reflection(game: Game, name: str, args: dict[str, Any]) -> str:
        insp = Inspector(game, args.get("mod") or None)
        limit = args.get("limit")
        if name == "call":
            call_args = args.get("args")
            if call_args is not None and not isinstance(call_args, list):
                raise ValueError("args must be a JSON array")
            kwargs = {"limit_ms": int(args["limit_ms"])} if args.get("limit_ms") else {}
            return render("call", insp.call(str(args["ref"]), str(args["function"]), call_args, args.get("lua_args") or None, **kwargs))
        if name == "snapshot":
            action = args.get("action")
            if action == "take":
                return render("snapshot", insp.snapshot(str(args["label"]), str(args["ref"]), args.get("pattern") or None))
            if action == "diff":
                return render("diff", insp.diff(str(args["label"]), args.get("ref") or None, bool(args.get("update"))))
            if action == "list":
                return render("snapshots", insp.snapshots())
            if action == "forget":
                return render("forget", insp.forget(args.get("label") or None))
            raise ValueError("action is take, diff, list or forget")
        what = args.get("what")
        ref = str(args.get("ref") or "")
        pattern = args.get("pattern") or None
        inherited = not args.get("own")
        if what == "world":
            return render("world", insp.world())
        if what == "find":
            return render("find", insp.find(ref, int(limit or 50)))
        if what == "props":
            return render("props", insp.props(ref, pattern, inherited, int(limit or 400)))
        if what == "functions":
            return render("functions", insp.functions(ref, pattern, inherited, int(limit or 300)))
        if what == "signature":
            return render("signature", insp.signature(ref, str(args.get("name") or "")))
        if what == "classes":
            return render("classes", insp.classes(pattern, args.get("base") or None, str(args.get("kind") or "class"), int(limit or 200)))
        if what == "target":
            return render("target", insp.target(float(args.get("distance") or 5000), args.get("ref") or None))
        raise ValueError("what is world, find, props, functions, signature, classes or target")

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:  # a notification: nothing to answer
            return None
        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion")
                version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "unreal-vibe-modding", "version": __version__},
                    "instructions": INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name", "")
                if name not in {t["name"] for t in TOOLS}:
                    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
                try:
                    result = self.call(name, params.get("arguments") or {})
                except LuaError as e:
                    result = text(f"ERROR: {e}", True)
                except (ConfigError, GameError, TransportError, ValueError, OSError, KeyError) as e:
                    result = text(f"{type(e).__name__}: {e}", True)
            else:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # never let one request take the server down
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve() -> int:
    server = Server()
    stdin = sys.stdin
    out = sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = server.handle(msg) if isinstance(msg, dict) else None
        if reply is not None:
            out.write(json.dumps(reply, ensure_ascii=False) + "\n")
            out.flush()
    return 0
