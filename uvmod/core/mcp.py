"""The MCP-server half every app shares: the stdio protocol (newline-delimited JSON-RPC 2.0) and the tools that need
only a Session. The app supplies its name and version, its instructions, the tools' texts, its extra tools and their
calls, the exceptions shown as tool errors, and the session the tools run against. Stdlib only."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
from typing import Any

from . import protocol
from .cli import status_text
from .config import ConfigError
from .session import GameError
from .transports import TransportError

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def text(content: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": content}], "isError": is_error}


def generic_tools(texts: dict[str, Any]) -> list[dict[str, Any]]:
    """The seven tools that work for every app: lua, mods, status, log, screenshot, push and game. The texts are the
    descriptions and the pieces of the schemas that differ, all supplied by the app; the keys are `lua`,
    `lua_limit_ms`, `lua_mod`, `mods`, `status`, `log`, `screenshot`, `screenshot_props` (the screenshot schema's
    properties), `push`, `game`, `game_actions` (the action enum), and `game_command` (absent: no console action)."""
    game_props: dict[str, Any] = {
        "action": {"type": "string", "enum": texts["game_actions"]},
        "timeout_s": {"type": "number", "minimum": 10, "maximum": 600},
    }
    if texts.get("game_command"):
        game_props["command"] = texts["game_command"]
    return [
        {
            "name": "lua",
            "description": texts["lua"],
            "inputSchema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Lua source; tried as `return <code>` first, then as statements."},
                    "mod": {"type": "string", "description": texts["lua_mod"]},
                    "limit_ms": {"type": "integer", "minimum": 1, "maximum": 5000, "description": texts["lua_limit_ms"]},
                },
                "required": ["code"],
            },
        },
        {"name": "mods", "description": texts["mods"], "inputSchema": {"type": "object", "properties": {}}},
        {"name": "status", "description": texts["status"], "inputSchema": {"type": "object", "properties": {}}},
        {
            "name": "log",
            "description": texts["log"],
            "inputSchema": {
                "type": "object",
                "properties": {"lines": {"type": "integer", "minimum": 1, "maximum": 2000}, "pattern": {"type": "string"}},
            },
        },
        {
            "name": "screenshot",
            "description": texts["screenshot"],
            "inputSchema": {"type": "object", "properties": texts["screenshot_props"]},
        },
        {
            "name": "push",
            "description": texts["push"],
            "inputSchema": {"type": "object", "properties": {"dir": {"type": "string"}}, "required": ["dir"]},
        },
        {
            "name": "game",
            "description": texts["game"],
            "inputSchema": {"type": "object", "properties": game_props, "required": ["action"]},
        },
    ]


class BaseServer:
    """What every app's MCP server is: the class attributes name the app (its serverInfo, its instructions, the
    generic tools' texts, its extra tools), make_session() builds its session on first use, and tool_errors are the
    exception types a tool call turns into a tool error."""

    name = ""
    version = ""
    instructions = ""
    texts: dict[str, Any] = {}
    extra_tools: list = []
    tool_errors: tuple = (ConfigError, GameError, TransportError, ValueError, OSError, KeyError)

    def __init__(self):
        self.game = None  # the session, made when the first tool needs it

    def make_session(self):
        raise NotImplementedError

    def session(self):
        if self.game is None:
            self.game = self.make_session()
        return self.game

    def tools(self) -> list[dict[str, Any]]:
        return generic_tools(self.texts) + list(self.extra_tools)

    def call_extra(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """The app's own tools; the base's call() hands the rest here."""
        raise KeyError(name)

    def tool_error(self, e: Exception) -> dict[str, Any]:
        return text(f"{type(e).__name__}: {e}", True)

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in ("lua", "mods", "status", "log", "screenshot", "push", "game"):
            return self.call_extra(name, args)  # the app's tools; some need no session (offline ones)
        game = self.session()
        if name in ("lua", "mods"):
            if name == "lua":
                result = game.exec(str(args.get("code", "")), args.get("mod") or None, args.get("limit_ms"))
            else:
                result = game.mods()
            if result.answer is None:
                return text(game.no_answer(result.delivery), True)
            via = "VibeBridge" if result.delivery == "bridge" else game.transport.engine.label
            in_game = f"{result.answer['elapsed_ms']:.2f} ms in the game, " if "elapsed_ms" in result.answer else ""
            footer = f"\n({via}, {in_game}{result.elapsed_s * 1000:.0f} ms round trip)"
            return text(protocol.render_text(result.answer) + footer, not result.answer.get("ok"))
        if name == "status":
            body, rc = status_text(game)
            return text(body, rc != 0)
        if name == "log":
            lines = game.log(int(args.get("lines") or 40), str(args.get("pattern") or ""))
            return text("\n".join(lines) or "(no matching lines)")
        if name == "screenshot":
            ui = args.get("ui") is not False
            if args.get("burst"):
                path = game.burst(int(args["burst"]), int(args.get("every_ms") or 750), mod=args.get("mod") or None, ui=ui)
                if os.path.isdir(path):
                    return text(f"no ffmpeg here to tile them; the frames are in {path}")
                got, asked = game.last_burst
                note = f"{got} of {asked} frames, numbered in order"
            else:
                path = game.screenshot(mod=args.get("mod") or None, ui=ui)
                note = ""
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            mime = "image/png" if path.endswith(".png") else "image/jpeg"
            caption = f"saved: {path}" + (f" ({note})" if note else "")
            return {"content": [{"type": "image", "data": data, "mimeType": mime}, {"type": "text", "text": caption}], "isError": False}
        if name == "push":
            return text(game.push(str(args["dir"])))
        if name == "game":
            action = args.get("action")
            timeout = float(args.get("timeout_s") or 180)
            if action == "launch":
                return text(game.launch())
            if action == "stop":
                return text(game.stop())
            if action == "crash":
                return text(game.crash_report())
            if action == "console":
                return text(game.console(str(args.get("command") or "")))
            if action in ("ready", "restart"):
                lines = []
                if action == "restart":
                    lines.append(game.stop())
                    time.sleep(2.0)
                    lines.append(game.launch())
                    time.sleep(10.0)
                ok, answer = game.ready(timeout)
                return text("\n".join(lines + [answer]), not ok)
            raise ValueError("action is launch, stop, restart, ready, crash or console")
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
                    "serverInfo": {"name": self.name, "version": self.version},
                    "instructions": self.instructions,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tools()}
            elif method == "tools/call":
                params = msg.get("params") or {}
                name = params.get("name", "")
                if name not in {t["name"] for t in self.tools()}:
                    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"unknown tool: {name}"}}
                try:
                    result = self.call(name, params.get("arguments") or {})
                except self.tool_errors as e:
                    result = self.tool_error(e)
            else:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # never let one request take the server down
            traceback.print_exc(file=sys.stderr)
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def serve(server) -> int:
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
