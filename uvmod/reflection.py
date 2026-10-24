"""Reading the game's reflection, and calling UFunctions behind a guard. The work runs in the game's Lua state
(lua/inspect.lua); this side sends calls and renders the answers for a person or an agent to read."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import helpers
from .game import Game


@dataclass
class Answer:
    """Parsed answer lines, by kind (see lua/inspect.lua for the line formats)."""

    object: str = ""
    rows: list[tuple[str, list[str]]] = field(default_factory=list)
    total: int | None = None
    shown: int | None = None

    def of(self, kind: str) -> list[list[str]]:
        return [f for k, f in self.rows if k == kind]


def parse(lines: list[str]) -> Answer:
    answer = Answer()
    for line in lines:
        if not line:
            continue
        kind, _, rest = line.partition("\t")
        fields = rest.split("\t") if rest else []
        if kind == "O":
            answer.object = rest
        elif kind == "N":
            answer.total, answer.shown = int(fields[0]), int(fields[1])
        else:
            answer.rows.append((kind, fields))
    return answer


def _opt(text: str | None) -> str:
    return helpers.lua_string(text) if text else "nil"


def _bool(flag: bool | None) -> str:
    return "nil" if flag is None else ("true" if flag else "false")


def parse_cli_args(values: list[str]) -> list:
    """CLI arguments for `call`: each is read as JSON if it parses (numbers, true, {"X": 1}, ...), else as text."""
    out = []
    for v in values:
        try:
            out.append(json.loads(v))
        except ValueError:
            out.append(v)
    return out


class Inspector:
    def __init__(self, game: Game, mod: str | None = None):
        self.game = game
        self.mod = mod

    def _call(self, expr: str, limit_ms: int = 3000) -> Answer:
        return parse(helpers.call(self.game, self.mod, helpers.INSPECT, expr, limit_ms))

    def world(self) -> Answer:
        return self._call("__uvmod_inspect.world()")

    def find(self, spec: str, limit: int = 50) -> Answer:
        return self._call(f"__uvmod_inspect.find({helpers.lua_string(spec)}, {int(limit)})")

    def props(self, ref: str, pattern: str | None = None, inherited: bool = True, limit: int = 400) -> Answer:
        return self._call(f"__uvmod_inspect.props({helpers.lua_string(ref)}, {_opt(pattern)}, {_bool(inherited)}, {int(limit)})")

    def functions(self, ref: str, pattern: str | None = None, inherited: bool = True, limit: int = 300) -> Answer:
        return self._call(f"__uvmod_inspect.functions({helpers.lua_string(ref)}, {_opt(pattern)}, {_bool(inherited)}, {int(limit)})")

    def signature(self, ref: str, name: str) -> Answer:
        return self._call(f"__uvmod_inspect.signature({helpers.lua_string(ref)}, {helpers.lua_string(name)})")

    def classes(self, pattern: str | None = None, base: str | None = None, kind: str = "class", limit: int = 200) -> Answer:
        return self._call(f"__uvmod_inspect.classes({_opt(pattern)}, {_opt(base)}, {helpers.lua_string(kind)}, {int(limit)})",
                          limit_ms=5000)

    def call(self, ref: str, name: str, args: list | None = None, lua_args: str | None = None,
             limit_ms: int = 2000) -> Answer:
        if args and lua_args:
            raise ValueError("pass either args or lua_args, not both")
        src = lua_args if lua_args else helpers.lua_args(args or [])
        return self._call(f"__uvmod_inspect.call({helpers.lua_string(ref)}, {helpers.lua_string(name)}, "
                          f"{helpers.lua_string(src)})", limit_ms)

    def snapshot(self, label: str, ref: str, pattern: str | None = None) -> Answer:
        return self._call(f"__uvmod_inspect.snapshot({helpers.lua_string(label)}, {helpers.lua_string(ref)}, {_opt(pattern)})")

    def diff(self, label: str, ref: str | None = None, update: bool = False) -> Answer:
        return self._call(f"__uvmod_inspect.diff({helpers.lua_string(label)}, {_opt(ref)}, {_bool(update)})")

    def snapshots(self) -> Answer:
        return self._call("__uvmod_inspect.snapshots()")

    def forget(self, label: str | None = None) -> Answer:
        return self._call(f"__uvmod_inspect.forget({_opt(label)})")

    def target(self, distance: float = 5000, ref: str | None = None) -> Answer:
        return self._call(f"__uvmod_inspect.target({float(distance)!r}, {_opt(ref)})")


# --- rendering --------------------------------------------------------------------------------------------------

def _cut(answer: Answer, what: str) -> list[str]:
    if answer.total is not None and answer.shown is not None and answer.total > answer.shown:
        return [f"({answer.total} {what} match, {answer.shown} shown: narrow it with a pattern or raise the limit)"]
    return []


def _fields(answer: Answer, kind: str, n: int) -> list[list[str]]:
    """The rows of one kind, each padded or cut to exactly n fields."""
    return [(f + [""] * n)[:n] for f in answer.of(kind)]


def _warnings(answer: Answer) -> list[str]:
    return [f"warning: {f[0]}" for f in answer.of("W") if f]


def render(op: str, answer: Answer) -> str:
    lines: list[str] = []
    if answer.object and op not in ("find", "classes", "snapshots"):
        lines.append(answer.object)
    if op == "find":
        for valid, name in _fields(answer, "X", 2):
            lines.append(name if valid == "true" else f"{name} (invalid)")
        lines += _cut(answer, "objects")
    elif op == "props":
        for owner, name, ptype, value in _fields(answer, "P", 4):
            lines.append(f"  {owner}.{name}: {ptype}" + (f" = {value}" if value else ""))
        lines += _cut(answer, "properties")
    elif op == "functions":
        for owner, name, flags, sig in _fields(answer, "F", 4):
            lines.append(f"  {owner}::{name}{sig}" + (f"  [{flags}]" if flags else ""))
        lines += _cut(answer, "functions")
    elif op == "signature":
        for owner, name, flags, sig in _fields(answer, "F", 4):
            lines.append(f"{owner}::{name}{sig}" + (f"  [{flags}]" if flags else ""))
        for index, name, ptype, role, pclass in _fields(answer, "A", 5):
            lines.append(f"  {index}. {name}: {ptype} ({role}, {pclass})")
        lines.append("(no property flags in UE4SS's Lua: in and out parameters look alike here; `uvmod api sig` has them)")
    elif op == "classes":
        for name, super_, path in _fields(answer, "K", 3):
            lines.append(f"{path}" + (f"  (: {super_})" if super_ else ""))
        lines += _cut(answer, "types")
    elif op == "call":
        for owner, name, flags, sig in _fields(answer, "F", 4):
            lines.append(f"called {owner}::{name}{sig}")
        lines += [f[0] for f in answer.of("V") if f] or ["(no return value)"]
    elif op in ("snapshot", "snapshots", "forget"):
        rows = answer.of("S")
        if not rows and op != "snapshot":
            lines.append("no snapshots")
        for label, taken, count, ref in ((f + [""] * 4)[:4] for f in rows):
            lines.append(f"snapshot {label}: {count} properties of {ref} (taken at unix time {taken})")
    elif op == "diff":
        changed = answer.of("D")
        lines.append(f"{len(changed)} propert{'y' if len(changed) == 1 else 'ies'} changed" if changed else "nothing changed")
        for name, before, after in ((f + [""] * 3)[:3] for f in changed):
            lines.append(f"  {name}: {before} -> {after}")
    else:  # world, target
        lines += [f[0] for f in answer.of("V") if f]
    lines += _warnings(answer)
    return "\n".join(lines)
