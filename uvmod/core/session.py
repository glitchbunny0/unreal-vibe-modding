"""One running game, reached through a transport: the requests, their journal, and the operations every app shares.
The engine-specific parts (screenshots through the engine, console commands, crash reports, what only the engine can
do) live in the app's subclass."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterator

from . import journal, protocol
from .config import Settings
from .transports import Status, Transport, make


class GameError(Exception):
    pass


@dataclass
class Result:
    delivery: str
    answer: dict[str, Any] | None
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return bool(self.answer and self.answer.get("ok"))


class Session:
    def __init__(self, settings: Settings, transport: Transport | None = None):
        self.settings = settings
        self.transport = transport or make(settings)

    def timeout_message(self, delivery: str, seconds: float) -> str:
        """Why nothing answered, after waiting `seconds`. The base text names no engine or mod loader; apps override
        it with their own."""
        if delivery == "missing":
            return "NO REPL: the LuaRepl folder is missing. Install VibeBridge, then reload the game's mods or start the game."
        if delivery == "bridge":
            return f"TIMEOUT after {seconds:g}s: VibeBridge did not answer."
        return f"TIMEOUT after {seconds:g}s: nothing answered."

    def no_answer(self, delivery: str, seconds: float | None = None) -> str:
        """Why nothing answered, in this app's terms."""
        return self.timeout_message(delivery, self.settings.timeout if seconds is None else seconds)

    def status_texts(self, st: Status) -> dict:
        """The status report's strings that name this app's engine or loader: `game_unknown` (the game line when its
        process cannot be found), `before_bridge` and `after_bridge` (lines around the VibeBridge state), and
        `repl_missing` (the hint when the LuaRepl folder is absent). The generic lines come from the status printer."""
        return {}

    def bridge_facts(self) -> list[str]:
        """Facts from the bridge's heartbeat shown after its state (e.g. the loader's version). Asked only when there is
        a heartbeat, because reading it can cost a round trip to the game's machine."""
        return []

    def request(self, request: dict[str, Any], timeout: float | None = None) -> Result:
        request_id = protocol.new_id()
        wait = self.settings.timeout if timeout is None else timeout
        journal.record(self.settings, {"id": request_id, "op": request.get("op"), "mod": request.get("mod", ""),
                                       "code": journal.short_code(str(request.get("code", "")))})
        t0 = time.monotonic()
        try:
            ex = self.transport.exchange(request_id, protocol.encode(request), protocol.encode(request, request_id), wait)
        except Exception as e:
            journal.record(self.settings, {"id": request_id, "done": "failed", "error": f"{type(e).__name__}: {e}"[:300]})
            raise
        elapsed = time.monotonic() - t0
        if ex.answer is None:
            journal.record(self.settings, {"id": request_id, "done": "timeout", "ms": round(elapsed * 1000)})
            return Result(ex.delivery, None, elapsed)
        answer = protocol.parse_answer(ex.answer)
        journal.record(self.settings, {"id": request_id, "done": "ok" if answer.get("ok") else "error",
                                       "ms": round(elapsed * 1000), "error": str(answer.get("error", ""))[:300]})
        return Result(ex.delivery, answer, elapsed)

    def mods(self) -> Result:
        return self.request(protocol.list_request())

    def resolve_mod(self, mod: str | None) -> str:
        """The mod to run code in: the one given, <PREFIX>_MOD, or the only mod the game offers."""
        if mod or self.settings.mod:
            return mod or self.settings.mod
        listed = self.mods()
        if not listed.answer:
            raise GameError(self.no_answer(listed.delivery))
        names = listed.answer.get("mods", [])
        if len(names) == 1:
            return names[0]
        raise GameError(f"several mods can run code; pick one with --mod or {self.settings.app.prefix}_MOD: {', '.join(names)}")

    def exec(self, code: str, mod: str | None = None, limit_ms: int | None = None) -> Result:
        target = self.resolve_mod(mod)
        limit = limit_ms if limit_ms is not None else self.settings.limit_ms
        return self.request(protocol.exec_request(target, code, limit))

    def status(self) -> Status:
        return self.transport.status(self.transport.engine.status_pattern)

    def log(self, lines: int = 40, pattern: str = "") -> list[str]:
        return self.transport.log(lines, pattern)

    def follow(self, pattern: str = "") -> Iterator[str]:
        return self.transport.follow(pattern)

    def push(self, local_dir: str) -> str:
        if not os.path.isdir(local_dir):
            raise GameError(f"not a directory: {local_dir}")
        return self.transport.push(local_dir.rstrip("/\\"))

    def query(self, handler: str, args: dict | None = None, mod: str | None = None) -> dict:
        """Run one of the bridge's ready-made game handlers in VibeBridge: {"ok": true, "result": ...} or
        {"ok": false, "error": ...}. Raises GameError when nothing answered or the Lua around the handler failed."""
        result = self.exec(f"return vb.query({protocol.lua_string(handler)}, {protocol.lua_value(args or {})})", mod)
        if result.answer is None:
            raise GameError(self.no_answer(result.delivery))
        if not result.ok:
            raise GameError(str(result.answer.get("error", "")))
        text = "".join(v[1:-1] if len(v) >= 2 and v[0] == '"' and v[-1] == '"' else v for v in result.answer.get("values", []))
        try:
            return json.loads(text)
        except ValueError as e:
            raise GameError(f"VibeBridge's answer is not JSON ({e}): {text[:200]}") from None

    # --- starting and stopping the game ----------------------------------------------------------------------------

    def launch(self) -> str:
        return self.transport.launch()

    def stop(self) -> str:
        return self.transport.stop()

    def ready(self, timeout: float = 180.0) -> tuple[bool, str]:
        """Wait until a REPL answers: the game is up and its Lua mods have started. (ok, what happened)."""
        t0 = time.monotonic()
        saved, self.settings.timeout = self.settings.timeout, 3.0
        try:
            tries = 0
            while True:
                tries += 1
                listed = self.mods()
                elapsed = time.monotonic() - t0
                if listed.answer and listed.answer.get("ok"):
                    who = "VibeBridge" if listed.delivery == "bridge" else f"{self.transport.engine.label} external REPL"
                    return True, f"ready after {elapsed:.0f} s ({who}; mods: {', '.join(listed.answer.get('mods', []))})"
                if elapsed >= timeout:
                    return False, f"NOT READY after {elapsed:.0f} s: {self.no_answer(listed.delivery, 3.0)}"
                if elapsed > 30 and tries % 5 == 0 and self.status().running is False:
                    return False, (f"the game is not running ({elapsed:.0f} s after waiting began); "
                                   f"see: {self.settings.app.name} crash")
                time.sleep(2.0)
        finally:
            self.settings.timeout = saved

    # --- screenshots through the window ----------------------------------------------------------------------------

    def window_screenshot(self, out: str | None = None) -> str:
        """The game's window copied from the screen, for engines with no screenshot call of their own; always with
        the UI, and only while the game is the window in front. Scaled like an engine screenshot. Returns the path."""
        out = out or os.path.join(tempfile.gettempdir(), time.strftime(f"{self.settings.app.name}-shot-%H%M%S.jpg"))
        png = os.path.splitext(out)[0] + ".png"
        self.transport.capture(png)
        return self._scaled(png, out)

    @staticmethod
    def _scaled(png: str, out: str) -> str:
        """`out` as a 1280 px wide JPEG made from `png` if ffmpeg is here (the PNG is then removed), else the PNG."""
        if shutil.which("ffmpeg") and not out.endswith(".png"):
            done = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", png, "-vf", "scale=1280:-1", out])
            if done.returncode == 0:
                os.remove(png)
                return out
        return png
