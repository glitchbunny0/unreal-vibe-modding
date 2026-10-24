"""Lua helpers that live in the game's Lua state (uvmod/lua/*.lua). Each defines one global table with a VERSION;
a call checks for it first and sends the helper along only when the state lacks it (first use, a mod restart, a new
version), so the helpers' state (watches, snapshots) survives between requests. Works through UE4SS's REPL and
through VibeBridge alike."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

from .game import Game, GameError, timeout_message

NEED_HELPER = "uvmod: send the helper"


class LuaError(Exception):
    """The game answered, and the Lua raised an error."""


def minify(source: str) -> str:
    """Drop the leading block comment, whole-line comments and indentation: the helper rides in a request of at most
    64 KiB. Code lines are left alone (a '--' inside a string stays)."""
    out = []
    in_header = source.lstrip().startswith("--[[")
    for raw in source.splitlines():
        text = raw.strip()
        if in_header:
            if "]]" in text:
                in_header = False
            continue
        if not text or text.startswith("--"):
            continue
        out.append(text)
    return "\n".join(out)


@dataclass(frozen=True)
class Helper:
    filename: str
    global_name: str

    @property
    def source(self) -> str:
        return (resources.files("uvmod") / "lua" / self.filename).read_text(encoding="utf-8")

    @property
    def version(self) -> str:
        """The helper's VERSION plus a hash of its source, so any edit counts as a new version: a Lua state that holds
        an older copy gets the new one (and the helper's own upgrade path runs) instead of keeping stale code."""
        source = self.source
        m = re.search(r"local VERSION = (\d+)", source)
        if not m:
            raise RuntimeError(f"{self.filename} has no 'local VERSION = N' line")
        return f"{m.group(1)}-{hashlib.sha1(source.encode('utf-8')).hexdigest()[:10]}"

    @property
    def payload(self) -> str:
        return re.sub(r"local VERSION = \d+", f'local VERSION = "{self.version}"', minify(self.source), count=1)


WATCH = Helper("watch.lua", "__uvmod")
INSPECT = Helper("inspect.lua", "__uvmod_inspect")
INPUT = Helper("input.lua", "__uvmod_input")
CENSUS = Helper("census.lua", "__uvmod_census")


def lua_string(text: str) -> str:
    """A Lua string literal for any text: printable ASCII stays, every other byte becomes a decimal escape."""
    out = []
    for byte in text.encode("utf-8"):
        if byte in (0x22, 0x5C):
            out.append("\\" + chr(byte))
        elif 0x20 <= byte < 0x7F:
            out.append(chr(byte))
        else:
            out.append(f"\\{byte:03d}")
    return '"' + "".join(out) + '"'


def lua_value(value) -> str:
    """A Lua literal for a JSON value: null -> nil, lists -> sequences, objects -> tables with string keys."""
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(lua_value(v) for v in value) + "}"
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            key = k if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(k)) else f"[{lua_string(str(k))}]"
            parts.append(f"{key} = {lua_value(v)}")
        return "{" + ", ".join(parts) + "}"
    raise ValueError(f"cannot pass {type(value).__name__} to Lua")


def lua_args(values) -> str:
    return ", ".join(lua_value(v) for v in values)


def call(game: Game, mod: str | None, helper: Helper, expr: str, limit_ms: int = 2000) -> list[str]:
    """Evaluate `expr` (which uses the helper's global) in `mod`'s Lua state; return the answer's lines."""
    version = helper.version
    check = (f'local H = rawget(_ENV, "{helper.global_name}") if type(H) ~= "table" or H.version ~= "{version}" then '
             f'return "{NEED_HELPER}" end\nreturn ' + expr)
    result = game.exec(check, mod, limit_ms)
    if result.answer and result.answer.get("ok") and result.answer.get("values") == [f'"{NEED_HELPER}"']:
        result = game.exec(helper.payload + "\nreturn " + expr, mod, limit_ms)
    if result.answer is None:
        raise GameError(timeout_message(result.delivery, game.settings.timeout))
    if not result.answer.get("ok"):
        raise LuaError(str(result.answer.get("error", "")).split("\nstack traceback:")[0].strip())
    lines: list[str] = []
    for value in result.answer.get("values", []):
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        lines.extend(value.split("\n"))
    return lines
