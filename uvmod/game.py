"""The core shared by the CLI and the MCP server: one running game, reached through a transport."""

from __future__ import annotations

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

    def console(self, command: str, mod: str | None = None) -> str:
        """Run an engine console command as the local player (open a map, ...). Shipping builds compile many out."""
        from .helpers import lua_string

        if not command.strip():
            raise GameError("a console command is needed")
        lua = (f"local pc = FindFirstOf(\"PlayerController\") if not pc:IsValid() then error(\"no player controller\") end "
               f"StaticFindObject(\"/Script/Engine.Default__KismetSystemLibrary\"):ExecuteConsoleCommand(pc, {lua_string(command)}, pc) "
               f"return \"ok\"")
        result = self.exec(lua, mod)
        if not result.ok:
            raise GameError(protocol.render_text(result.answer) if result.answer else timeout_message(result.delivery, self.settings.timeout))
        return f"ran: {command}"

    def burst(self, count: int = 6, every_ms: int = 200, out: str | None = None, mod: str | None = None) -> str:
        """`count` engine screenshots `every_ms` apart, scheduled inside the game (so the timing is the game's, not the
        transport's), copied here and tiled into one contact sheet. Returns its path (or a folder of PNGs without
        ffmpeg)."""
        if not 2 <= count <= 16:
            raise GameError("a burst is 2 to 16 screenshots")
        before = set(self.transport.recent_shots(count + 4))
        lua = f"""
local pc = FindFirstOf("PlayerController")
local ksl = StaticFindObject("/Script/Engine.Default__KismetSystemLibrary")
local function shot() if pc:IsValid() then ksl:ExecuteConsoleCommand(pc, "shot", pc) end end
shot()
if type(ExecuteInGameThreadWithDelay) == "function" then
  for i = 1, {count - 1} do ExecuteInGameThreadWithDelay(i * {every_ms}, shot) end
else
  local k = 1
  LoopAsync({every_ms}, function() k = k + 1 ExecuteInGameThread(shot) return k >= {count} end)
end
return "ok"
"""
        result = self.exec(lua, mod)
        if not result.ok:
            raise GameError(protocol.render_text(result.answer) if result.answer else timeout_message(result.delivery, self.settings.timeout))
        # The engine writes one screenshot at a time and drops requests meanwhile (RV There Yet?, 2560x1440: one
        # PNG per ~0.73 s), so stop waiting once nothing new arrived for a while after the last scheduled shot.
        t0 = time.monotonic()
        expected_end = t0 + count * every_ms / 1000
        deadline = expected_end + 15
        new: list[str] = []
        last_change = t0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            now_new = [p for p in self.transport.recent_shots(count + 4) if p not in before]
            if len(now_new) != len(new):
                new, last_change = now_new, time.monotonic()
            if len(new) >= count or (new and time.monotonic() > expected_end and time.monotonic() - last_change > 2.5):
                break
        if not new:
            raise GameError("no new screenshots appeared (set UVMOD_SHOTS if the game saves them elsewhere)")
        time.sleep(0.5)  # the last PNG may still be writing
        folder = tempfile.mkdtemp(prefix="uvmod-burst-")
        for i, path in enumerate(sorted(new)[:count], 1):
            self.transport.fetch(path, os.path.join(folder, f"frame_{i:02d}.png"))
        got = min(len(new), count)
        self.last_burst = (got, count)
        out = out or os.path.join(tempfile.gettempdir(), time.strftime("uvmod-burst-%H%M%S.jpg"))
        if shutil.which("ffmpeg"):
            cols = 3 if got > 4 else 2
            rows = (got + cols - 1) // cols
            done = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "1", "-i", os.path.join(folder, "frame_%02d.png"),
                                   "-vf", f"scale=640:-1,drawtext=text='%{{frame_num}}':start_number=1:x=8:y=8:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.5,tile={cols}x{rows}",
                                   "-frames:v", "1", out], capture_output=True)
            if done.returncode != 0:  # drawtext needs a font; tile without the numbers
                done = subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-framerate", "1", "-i", os.path.join(folder, "frame_%02d.png"),
                                       "-vf", f"scale=640:-1,tile={cols}x{rows}", "-frames:v", "1", out], capture_output=True)
            if done.returncode == 0:
                shutil.rmtree(folder, ignore_errors=True)
                return out
        return folder

    # --- starting and stopping the game, and what to do when it died ---------------------------------------------

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
                    who = "VibeBridge" if listed.delivery == "bridge" else "UE4SS external REPL"
                    return True, f"ready after {elapsed:.0f} s ({who}; mods: {', '.join(listed.answer.get('mods', []))})"
                if elapsed >= timeout:
                    return False, f"NOT READY after {elapsed:.0f} s: {timeout_message(listed.delivery, 3.0)}"
                if elapsed > 30 and tries % 5 == 0 and self.status().running is False:
                    return False, f"the game is not running ({elapsed:.0f} s after waiting began); see: uvmod crash"
                time.sleep(2.0)
        finally:
            self.settings.timeout = saved

    def crash_report(self, journal_count: int = 6) -> str:
        """Everything that says why the game died: is it running, the newest crash folder (Unreal's CrashContext and
        minidump), UE4SS's own crash dump, the end of UE4SS.log, and the last requests uvmod sent."""
        from .crash import parse_crash_context, parse_minidump

        now = time.time()
        out: list[str] = []
        st = self.status()
        out.append("game: " + ("running" if st.running else "not running" if st.running is False else "unknown")
                   + (f" ({st.process}, started {st.started})" if st.running else ""))
        files = self.transport.crash_files()
        folders, dumps = files.get("folders") or [], files.get("dumps") or []
        tmp = tempfile.mkdtemp(prefix="uvmod-crash-")

        def age(t: float) -> str:
            m = max(0, int((now - t) / 60))
            return f"{m} min ago" if m < 120 else time.strftime("%Y-%m-%d %H:%M", time.localtime(t))

        def minidump(path: str) -> list[str]:
            local = os.path.join(tmp, os.path.basename(path.replace("\\", "/")))
            try:
                self.transport.fetch(path, local)
                with open(local, "rb") as f:
                    return parse_minidump(f.read()).describe()
            except Exception as e:  # a damaged or locked dump should not hide the rest of the report
                return [f"minidump: could not read {path}: {e}"]

        if st.running and (folders or dumps):
            out.append("(the game is running now: the crash evidence below is from an earlier session)")
        if folders:
            f = folders[0]
            path = f["path"].rstrip("/\\")
            out.append(f"\nnewest Unreal crash folder ({age(f['time'])}): {path}")
            names = f.get("files") or []
            if "CrashContext.runtime-xml" in names:
                try:
                    ctx = parse_crash_context(self.transport.read_text(path + "/CrashContext.runtime-xml"))
                    if ctx.error:
                        out.append(f"error: {ctx.error}")
                    if ctx.engine:
                        out.append(f"engine: {ctx.engine}" + ("" if ctx.has_ue4ss is None else
                                                              f"; UE4SS.dll loaded: {'yes' if ctx.has_ue4ss else 'no'}"))
                    if ctx.callstack:
                        out.append("call stack (Unreal's, unsymbolized frames are module+offset):")
                        out += ["  " + l for l in ctx.callstack[:25]]
                except Exception as e:
                    out.append(f"CrashContext.runtime-xml: could not read it: {e}")
            dmp = next((n for n in names if n.lower().endswith(".dmp")), None)
            if dmp:
                described = minidump(path + "/" + dmp)
                if any(l.startswith("call stack") for l in out):  # Unreal's own stack says more; keep the scan short
                    described = described[:16]
                out += described
        else:
            out.append("\nno Unreal crash folder")
        if dumps and (not folders or dumps[0]["time"] >= folders[0]["time"] - 120):
            d = dumps[0]
            out.append(f"\nUE4SS crash dump ({age(d['time'])}): {d['path']}")
            out += minidump(d["path"])
        out.append("\nUE4SS.log, last lines (without the external REPL's per-request lines):")
        out += ["  " + l for l in [l for l in self.log(400) if "[ExternalLuaRepl] Request " not in l][-25:]]
        entries = journal.tail(self.settings, journal_count)
        out.append("\nlast requests uvmod sent (a request with no answer right before the crash is the first suspect):")
        out += ["  " + l for l in journal.render(entries).splitlines()]
        if any("ue4ss.dll+" in l.lower() for l in out):
            out.append("\nUE4SS.dll offsets symbolize with the PDB of the same build: "
                       "llvm-symbolizer --obj=UE4SS.dll --relative-address --functions=linkage --demangle 0x...")
        shutil.rmtree(tmp, ignore_errors=True)
        return "\n".join(out)

    # --- the reflection index -------------------------------------------------------------------------------------

    def build_index(self, from_file: str | None = None, blueprints: bool = True, keep: bool = False) -> str:
        """Dump the game's reflection with UE4SS's DumpJMAP, copy it here and build the SQLite index from it (or
        build from a .jmap file already here). Returns a report."""
        from . import index

        folder = index.folder()
        folder.mkdir(parents=True, exist_ok=True)
        lines = []
        if from_file:
            jmap = from_file
        else:
            t0 = time.monotonic()
            saved, self.settings.timeout = self.settings.timeout, 900.0
            try:
                result = self.exec(f"DumpJMAP({'true' if blueprints else 'false'}) return 'ok'", None, 5000)
            finally:
                self.settings.timeout = saved
            if result.answer is None:
                raise GameError(timeout_message(result.delivery, 900.0))
            if not result.ok:
                err = str(result.answer.get("error", ""))
                if "DumpJMAP" in err and "nil" in err:
                    raise GameError("this UE4SS build has no DumpJMAP (UE4SS experimental 1181 and newer have it)")
                raise GameError(f"DumpJMAP failed: {err.splitlines()[0] if err else '?'}")
            dumps = self.transport.files("*.jmap")
            if not dumps:
                raise GameError("DumpJMAP ran, but no .jmap file is in the ue4ss folder (see: uvmod log 20 JMapGenerator)")
            remote = dumps[0]["path"]
            lines.append(f"dumped in {time.monotonic() - t0:.0f} s: {remote} ({dumps[0]['size'] // (1 << 20)} MB, left in place)")
            jmap = str(folder / os.path.basename(remote.replace("\\", "/")))
            t1 = time.monotonic()
            self.transport.fetch_large(remote, jmap)
            lines.append(f"copied in {time.monotonic() - t1:.0f} s")
        t2 = time.monotonic()
        db = folder / index.index_name(os.path.basename(jmap))
        counts = index.build(jmap, str(db))
        lines.append(f"indexed in {time.monotonic() - t2:.0f} s: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        lines.append(f"index: {db}")
        if not from_file and not keep:
            os.remove(jmap)
        return "\n".join(lines)
