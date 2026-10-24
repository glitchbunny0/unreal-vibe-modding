"""Watching UFunction calls: hook once, let the player act, then read what was recorded (`events`) or block
until something happens (`wait`). The recording runs in the game's Lua state (lua/watch.lua); this side
only sends calls and reads their answers, so it works through UE4SS's REPL and through VibeBridge alike."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from importlib import resources

from .game import Game, GameError, timeout_message

HELPER = (resources.files("uvmod") / "lua" / "watch.lua").read_text(encoding="utf-8")
HELPER_VERSION = int(re.search(r"local VERSION = (\d+)", HELPER).group(1))
NEED_HELPER = "uvmod: send the helper"
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
MAX_WAIT_SECONDS = 600.0


class LuaError(Exception):
    """The game answered, and the Lua raised an error."""


@dataclass
class Event:
    seq: int
    clock: float
    watch: str
    text: str


@dataclass
class Watch:
    id: str
    when: str
    calls: int
    recorded: int
    limit: int
    errors: int
    function: str
    last_error: str


@dataclass
class Batch:
    cursor: int = 0
    lost: int = 0
    pending: int = 0
    active: list[str] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    watches: list[Watch] = field(default_factory=list)


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


def parse(lines: list[str]) -> Batch:
    batch = Batch()
    for line in lines:
        kind, _, rest = line.partition("\t")
        if kind == "C":
            cursor, lost, pending = rest.split("\t")[:3]
            batch.cursor, batch.lost, batch.pending = int(cursor), int(lost), int(pending)
        elif kind == "L":
            batch.active = [i for i in rest.split(",") if i]
        elif kind == "E":
            seq, clock, watch, text = rest.split("\t", 3)
            batch.events.append(Event(int(seq), float(clock), watch, text))
        elif kind == "W":
            f = rest.split("\t", 7) + [""] * 8
            batch.watches.append(Watch(f[0], f[1], int(f[2]), int(f[3]), int(f[4]), int(f[5]), f[6], f[7]))
    return batch


class Watcher:
    def __init__(self, game: Game, mod: str | None = None):
        self.game = game
        self.mod = mod

    def _call(self, call: str, limit_ms: int = 2000) -> Batch:
        # The helper (~11 KB) goes along only when this Lua state lacks it (first use, a mod restart, a new version).
        check = (f'local U = rawget(_ENV, "__uvmod") if type(U) ~= "table" or U.version ~= {HELPER_VERSION} then '
                 f'return "{NEED_HELPER}" end\nreturn ' + call)
        result = self.game.exec(check, self.mod, limit_ms)
        if result.answer and result.answer.get("ok") and result.answer.get("values") == [f'"{NEED_HELPER}"']:
            result = self.game.exec(HELPER + "\nreturn " + call, self.mod, limit_ms)
        if result.answer is None:
            raise GameError(timeout_message(result.delivery, self.game.settings.timeout))
        if not result.answer.get("ok"):
            raise LuaError(str(result.answer.get("error", "")).split("\nstack traceback:")[0].strip())
        lines: list[str] = []
        for value in result.answer.get("values", []):
            if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                value = value[1:-1]
            lines.extend(value.split("\n"))
        return parse(lines)

    def watch(self, function: str, watch_id: str | None = None, capture: str | None = None, post: bool = False,
              limit: int = 200) -> Batch:
        watch_id = watch_id or default_id(function)
        if not ID_PATTERN.match(watch_id):
            raise ValueError("a watch id is 1-40 letters, digits, _ . -")
        if not 1 <= limit <= 100000:
            raise ValueError("limit must be between 1 and 100000")
        args = [lua_string(watch_id), lua_string(function), lua_string(capture) if capture else "nil",
                lua_string("post" if post else "pre"), str(limit)]
        return self._call(f"__uvmod.watch({', '.join(args)})")

    def unwatch(self, watch_id: str | None = None) -> Batch:
        return self._call(f"__uvmod.unwatch({lua_string(watch_id)})" if watch_id else "__uvmod.unwatch_all()")

    def list(self) -> Batch:
        return self._call("__uvmod.list()")

    def events(self, since: int | None = None, limit: int = 200) -> Batch:
        """New events since the last drain (or `since`), at most `limit` (the buffer keeps the last 1000)."""
        total = Batch()
        cursor = since
        while True:
            want = min(200, limit - len(total.events))
            batch = self._call(f"__uvmod.drain({'nil' if cursor is None else int(cursor)}, {want})", limit_ms=1000)
            total.lost += batch.lost if not total.events else 0
            total.events += batch.events
            total.cursor, total.pending, total.active, total.watches = batch.cursor, batch.pending, batch.active, batch.watches
            cursor = batch.cursor
            if batch.pending == 0 or not batch.events or len(total.events) >= limit:
                return total

    def wait(self, watch_id: str | None = None, count: int = 1, until: str | None = None, timeout: float = 30.0,
             poll: float = 0.25) -> tuple[bool, Batch | str, float]:
        """Block until `count` new events (of `watch_id`, or of any watch) or until the Lua expression `until`
        is truthy. Returns (met, the events or the expression's last value, seconds waited)."""
        timeout = max(0.0, min(timeout, MAX_WAIT_SECONDS))
        t0 = time.monotonic()
        deadline = t0 + timeout
        if until:
            while True:
                result = self.game.exec(f"return ({until})", self.mod)
                if result.answer is None:
                    raise GameError(timeout_message(result.delivery, self.game.settings.timeout))
                if not result.answer.get("ok"):
                    raise LuaError(str(result.answer.get("error", "")).split("\nstack traceback:")[0].strip())
                values = result.answer.get("values") or ["nil"]
                if values[0] not in ("nil", "false"):
                    return True, values[0], time.monotonic() - t0
                if time.monotonic() >= deadline:
                    return False, values[0], time.monotonic() - t0
                time.sleep(poll)
        seen = Batch()
        while True:
            batch = self.events()
            if not batch.active:
                raise GameError("nothing is being watched; start a watch first (it is gone if the mod restarted)")
            if watch_id and watch_id not in batch.active:
                raise GameError(f"no watch named '{watch_id}' (watching: {', '.join(batch.active)})")
            seen.lost += batch.lost
            seen.events += batch.events
            seen.cursor, seen.pending, seen.active, seen.watches = batch.cursor, batch.pending, batch.active, batch.watches
            matched = sum(1 for e in seen.events if watch_id is None or e.watch == watch_id)
            if matched >= count:
                return True, seen, time.monotonic() - t0
            if time.monotonic() >= deadline:
                return False, seen, time.monotonic() - t0
            time.sleep(poll)


def default_id(function: str) -> str:
    """The function's own name, as an id: /Script/Engine.Actor:ReceiveTick -> ReceiveTick."""
    name = re.split(r"[:.]", function.rstrip(":."))[-1]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:40]
    return name or "watch"


def render_events(batch: Batch) -> str:
    """Compact, for an agent to read: runs of identical calls collapse into one line."""
    events = batch.events
    lines = [f"{len(events)} new event{'s' if len(events) != 1 else ''}" if events else "no new events"]
    lines[0] += f" (cursor {batch.cursor}; watching: {', '.join(batch.active) or 'nothing'})"
    if batch.lost:
        lines.append(f"({batch.lost} older events were dropped: the game keeps the last 1000)")
    t0 = events[0].clock if events else 0.0
    i = 0
    while i < len(events):
        j = i
        while j + 1 < len(events) and (events[j + 1].watch, events[j + 1].text) == (events[i].watch, events[i].text):
            j += 1
        a, b = events[i], events[j]
        if i == j:
            lines.append(f"#{a.seq} +{a.clock - t0:.2f}s {a.watch}: {a.text}")
        else:
            lines.append(f"#{a.seq}-#{b.seq} +{a.clock - t0:.2f}s..+{b.clock - t0:.2f}s {a.watch}: {a.text} (x{j - i + 1})")
        i = j + 1
    if batch.pending:
        lines.append(f"({batch.pending} more events waiting: call events again)")
    for w in batch.watches:
        if w.recorded >= w.limit:
            lines.append(f"watch {w.id} stopped recording at its limit of {w.limit} ({w.calls} calls so far); "
                         "watch it again to record more")
        if w.errors:
            lines.append(f"watch {w.id}: {w.errors} capture error(s), last: {w.last_error}")
    return "\n".join(lines)


def render_wait(met: bool, result: Batch | str, seconds: float) -> str:
    if isinstance(result, str):
        if met:
            return f"true after {seconds:.1f}s: => {result}"
        return f"TIMEOUT after {seconds:.1f}s: still => {result}"
    head = f"seen after {seconds:.1f}s" if met else f"TIMEOUT after {seconds:.1f}s: not seen yet"
    return head + "\n" + render_events(result)


def render_watches(batch: Batch) -> str:
    if not batch.watches:
        return "nothing is being watched"
    lines = []
    for w in batch.watches:
        line = f"{w.id}: {w.function} ({w.when}) {w.calls} calls, {w.recorded}/{w.limit} recorded"
        if w.errors:
            line += f", {w.errors} errors, last: {w.last_error}"
        lines.append(line)
    return "\n".join(lines)
