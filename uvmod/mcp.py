"""MCP server on stdio (newline-delimited JSON-RPC 2.0) over the same core as the CLI. Stdlib only."""

from __future__ import annotations

import base64
import json
import os
import sys
import traceback
from typing import Any

from . import __version__, protocol
from .config import ConfigError, Settings
from .game import Game, GameError, timeout_message
from .transports import TransportError

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
Also: a property's name is a guess about its meaning, so measure what it does before relying on it. Keep loops
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
        "description": "Takes an engine screenshot of the game as the player sees it and returns the image. Look at the game yourself instead of asking the player to describe it.",
        "inputSchema": {"type": "object", "properties": {"mod": {"type": "string"}}},
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
            path = game.screenshot(mod=args.get("mod") or None)
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            mime = "image/png" if path.endswith(".png") else "image/jpeg"
            return {"content": [{"type": "image", "data": data, "mimeType": mime}, {"type": "text", "text": f"saved: {path}"}], "isError": False}
        if name == "push":
            return text(game.push(str(args["dir"])))
        if name == "swap":
            ok, answer = game.swap(str(args["dir"]))
            return text(answer, not ok)
        raise KeyError(name)

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
