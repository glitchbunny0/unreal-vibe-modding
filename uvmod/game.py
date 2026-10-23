"""The core shared by the CLI and the MCP server: one running game, reached through a transport."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterator

from . import protocol
from .config import Settings
from .transports import Status, Transport, make

# Log lines `status` shows: the UE4SS version line, ExternalLuaRepl (not its per-request lines, which verbose
# logging writes for every call), VibeBridge, hot-swap shells. Valid in Python and .NET regular expressions.
STATUS_PATTERN = r"UE4SS - v|\[ExternalLuaRepl\] (?!Request )|\[VibeBridge\]|loaded WITHOUT logic|swapped logic|swap REFUSED"

SCREENSHOT_LUA = (
    'local pc = FindFirstOf("PlayerController") '
    'StaticFindObject("/Script/Engine.Default__KismetSystemLibrary"):ExecuteConsoleCommand(pc, "shot", pc) '
    'return "ok"'
)


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


def timeout_message(delivery: str, seconds: float) -> str:
    if delivery == "missing":
        return ("NO REPL: the game's ue4ss folder has no LuaRepl folder. Use a UE4SS build with the external Lua REPL and "
                "[Debug] ExternalLuaReplEnabled = 1, or install the VibeBridge mod (uvmod install-bridge), then start the game.")
    if delivery == "bridge":
        return f"TIMEOUT after {seconds:g}s: VibeBridge did not answer (the game thread may be stuck, or loading)."
    return (f"TIMEOUT after {seconds:g}s: nothing answered. The game is not running, its UE4SS has no external REPL turned on "
            "and VibeBridge is not running, or the game thread is stuck.")


class Game:
    def __init__(self, settings: Settings, transport: Transport | None = None):
        self.settings = settings
        self.transport = transport or make(settings)

    def request(self, request: dict[str, Any], timeout: float | None = None) -> Result:
        request_id = protocol.new_id()
        wait = self.settings.timeout if timeout is None else timeout
        t0 = time.monotonic()
        ex = self.transport.exchange(request_id, protocol.encode(request), protocol.encode(request, request_id), wait)
        elapsed = time.monotonic() - t0
        if ex.answer is None:
            return Result(ex.delivery, None, elapsed)
        return Result(ex.delivery, protocol.parse_answer(ex.answer), elapsed)

    def mods(self) -> Result:
        return self.request(protocol.list_request())

    def resolve_mod(self, mod: str | None) -> str:
        """The mod to run code in: the one given, UVMOD_MOD, or the only mod the game offers."""
        if mod or self.settings.mod:
            return mod or self.settings.mod
        listed = self.mods()
        if not listed.answer:
            raise GameError(timeout_message(listed.delivery, self.settings.timeout))
        names = listed.answer.get("mods", [])
        if len(names) == 1:
            return names[0]
        raise GameError(f"several mods can run code; pick one with --mod or UVMOD_MOD: {', '.join(names)}")

    def exec(self, code: str, mod: str | None = None, limit_ms: int | None = None) -> Result:
        target = self.resolve_mod(mod)
        limit = limit_ms if limit_ms is not None else self.settings.limit_ms
        return self.request(protocol.exec_request(target, code, limit))

    def status(self) -> Status:
        return self.transport.status(STATUS_PATTERN)

    def log(self, lines: int = 40, pattern: str = "") -> list[str]:
        return self.transport.log(lines, pattern)

    def follow(self, pattern: str = "") -> Iterator[str]:
        return self.transport.follow(pattern)

    def push(self, local_dir: str) -> str:
        if not os.path.isdir(local_dir):
            raise GameError(f"not a directory: {local_dir}")
        return self.transport.push(local_dir.rstrip("/\\"))

    def swap(self, local_dir: str) -> tuple[bool, str]:
        """Push a hot-swap-shell mod and swap its logic in place. (ok, the shell's answer or why there was none)."""
        if not os.path.isdir(local_dir):
            raise GameError(f"not a directory: {local_dir}")
        local_dir = local_dir.rstrip("/\\")
        answer = self.transport.swap(local_dir, protocol.new_id(), self.settings.timeout)
        if answer is None:
            return False, (f"TIMEOUT after {self.settings.timeout:g}s: the files are pushed, but the running "
                           f"{os.path.basename(local_dir)} did not answer. It is not a hot-swap shell yet (restart the "
                           "game once to load it), or the game thread is stuck.")
        return answer.startswith("OK"), answer.strip()

    def screenshot(self, out: str | None = None, mod: str | None = None) -> str:
        """Take an engine screenshot ("shot"), copy it here, scale it to 1280 px wide if ffmpeg exists. Returns the path."""
        before = self.transport.newest_shot()
        result = self.exec(SCREENSHOT_LUA, mod)
        if not result.ok:
            raise GameError(protocol.render_text(result.answer) if result.answer else timeout_message(result.delivery, self.settings.timeout))
        now = None
        for _ in range(20):
            time.sleep(0.5)
            now = self.transport.newest_shot()
            if now and now != before:
                break
        if not now or now == before:
            raise GameError("no new screenshot appeared (set UVMOD_SHOTS if the game saves them elsewhere)")
        time.sleep(0.5)  # let the PNG finish writing
        out = out or os.path.join(tempfile.gettempdir(), time.strftime("uvmod-shot-%H%M%S.jpg"))
        png = os.path.splitext(out)[0] + ".png"
        self.transport.fetch(now[0], png)
        if shutil.which("ffmpeg") and not out.endswith(".png"):
            done = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", png, "-vf", "scale=1280:-1", out])
            if done.returncode == 0:
                os.remove(png)
                return out
        return png
