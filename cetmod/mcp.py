"""cetmod's MCP server on stdio (newline-delimited JSON-RPC 2.0), on core's BaseServer: the generic tools with CET's
texts plus `query`. Stdlib only."""

from __future__ import annotations

from typing import Any

from . import APP, __version__
from .config import Settings
from .game import Game
from .handlers import HANDLERS, catalog, render
from uvmod.core.mcp import BaseServer, serve as serve_core, text

INSTRUCTIONS = """Drives a running Cyberpunk 2077 through Cyber Engine Tweaks (the VibeBridge CET mod): run Lua in the game,
call ready-made game handlers (`query`), read CET's log, take screenshots of the game window, push CET mods. Start with
`status`. Code runs with the full power of a CET mod inside the player's game: a mistake can crash or freeze it, and CET
cannot stop a request that never ends. Read before you write, keep changes small, look at the result after each one,
and ask the player before changing their save (items, quest facts, levels)."""

LUA_RULES = """Runs Lua (LuaJIT: Lua 5.1) inside the VibeBridge CET mod, in onUpdate on the game's thread, and returns
printed lines, return values (display strings) or the error (without a traceback: CET gives mods no debug library).
Globals persist between calls; locals do not. CET's API is there: Game.GetPlayer(), Game.GetStatsSystem(),
Game.GetTransactionSystem() and the rest of the game's script API (NativeDB lists it), TweakDB:GetFlat/SetFlat, NewObject,
ObserveAfter/Override, Dump/DumpType/GameDump, GetMod(name). `vb.json(value)` returns JSON for any value (tables, game
objects, CET's types, 64-bit numbers); `return vb.chunks(vb.json(x))` for anything over 4 KB.
Rules: limit_ms is not enforced (CET cannot interrupt Lua), so never loop without a bound. Game.GetPlayer() is nil in
menus and loading screens. 64-bit numbers are LuaJIT cdata (tonumber() them for maths). Observe/Override callbacks stay
until CET reloads its mods: register them once and remember what you added."""

# The texts of the generic tools, as `generic_tools` wants them.
TEXTS: dict[str, Any] = {
    "lua": LUA_RULES,
    "lua_limit_ms": "Checked but not enforced by CET; default 250.",
    "lua_mod": "Mod whose Lua state runs the code (see `mods`). Default: CETMOD_MOD or the only mod.",
    "mods": "Lists the mods whose Lua state `lua` can run code in.",
    "status": "Is the game running, which REPL answers (UE4SS's own or the VibeBridge mod), the settings that matter.",
    "log": "The last lines of CET's scripting.log (mods loading, their prints and errors), optionally only those matching a regular expression (case-insensitive).",
    "screenshot": ("Copies the game's window from the screen as the player sees it (HUD and menus included) and returns "
                   "the image. Look at the game yourself instead of asking the player to describe it. While another "
                   "window is in front it refuses (the screen would show the player's other programs): ask the player "
                   "to switch to the game."),
    "screenshot_props": {},
    "push": "Copies a CET mod folder (with init.lua) from this machine into CET's mods/ folder. CET loads it when it reloads its mods (overlay: Reload all mods) or at the next game start.",
    "game": (
        "Starts and stops the game, and finds out why it died. `launch` starts it (the game's exe, or the CETMOD_LAUNCH "
        "command), `stop` ends its process, `restart` does both and waits until VibeBridge answers (that is how new "
        "builds of CET mods with hooks load), `ready` waits for VibeBridge (up to `timeout_s`), `crash` reports why it "
        "died: REDEngine's newest crash report (stack trace, report, the minidump's exception), the end of CET's "
        "scripting.log, and the last requests sent from here (the one without an answer is the first suspect). After "
        "a crash, read `crash` before restarting, and do not repeat the call that killed it."),
    "game_actions": ["launch", "stop", "restart", "ready", "crash"],
}

QUERY_TOOL: dict[str, Any] = {
    "name": "query",
    "description": ("Runs one of CETBridge's ready-made game handlers in the game and returns its result as JSON. "
                    "Handlers marked as changing the game change the player's game (and their save once it is saved): "
                    "ask the player first.\n" + catalog()),
    "inputSchema": {
        "type": "object",
        "properties": {
            "handler": {"type": "string", "enum": sorted(HANDLERS)},
            "args": {"type": "object", "description": "The handler's arguments by name, e.g. {\"x\": 1, \"y\": 2, \"z\": 3}."},
            "mod": {"type": "string"},
        },
        "required": ["handler"],
    },
}


class Server(BaseServer):
    name = APP.name
    version = __version__
    instructions = INSTRUCTIONS
    texts = TEXTS
    extra_tools = [QUERY_TOOL]

    def make_session(self) -> Game:
        return Game(Settings.load())

    def call_extra(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "query":
            answer = self.session().query(str(args.get("handler") or ""), args.get("args") or {}, args.get("mod") or None)
            return text(render(answer), not answer.get("ok"))
        raise KeyError(name)


def serve() -> int:
    return serve_core(Server())
