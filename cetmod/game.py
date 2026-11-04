"""cetmod's game: Cyberpunk 2077 through Cyber Engine Tweaks. CET has no screenshot call, no console commands and no
way to stop a running request; the parts every app shares come from core's Session."""

from __future__ import annotations

import shutil
import tempfile
import time

from uvmod.core import journal
from uvmod.core.minidump import parse_minidump
from uvmod.core.session import GameError, Session

# Module names whose addresses count as minidump stack frames (core leaves that choice to the caller); kept from when
# this report lived in uvmod, so the reports read the same.
INTERESTING = ("ue4ss.dll", "shipping.exe", "main.dll")


def timeout_message(delivery: str, seconds: float) -> str:
    if delivery == "missing":
        return ("NO BRIDGE: CET's mods/VibeBridge/LuaRepl folder is missing. Install VibeBridge (cetmod install-bridge), "
                "then reload CET's mods (overlay: Reload all mods) or start the game.")
    if delivery == "bridge":
        return (f"TIMEOUT after {seconds:g}s: VibeBridge did not answer (the game may be loading or stuck, or a request "
                "that never ends is running: CET cannot stop one).")
    return (f"TIMEOUT after {seconds:g}s: nothing answered. The game is not running, or VibeBridge is not loaded "
            "(reload CET's mods after installing it; see: cetmod status).")


class Game(Session):
    def timeout_message(self, delivery: str, seconds: float) -> str:
        return timeout_message(delivery, seconds)

    def bridge_facts(self) -> list[str]:
        """CET writes info lines to its logs only when a warning flushes them, so the heartbeat says more."""
        info = self.transport.bridge_info() or {}
        facts = [f"v{info['version']}" if info.get("version") else "", f"CET {info['cet']}" if info.get("cet") else "",
                 f"{info['handlers']} CETBridge handlers" if info.get("handlers") else ""]
        return [f for f in facts if f]

    def status_texts(self, st) -> dict:
        return {"game_unknown": "game: unknown (no *-Shipping.exe beside the ue4ss folder; set CETMOD_PROCESS)",
                "repl_missing": "repl: no LuaRepl folder in mods/VibeBridge (cetmod install-bridge)"}

    def screenshot(self, out: str | None = None, mod: str | None = None, ui: bool = True) -> str:
        """The game's window copied from the screen, as the player sees it (CET has no screenshot call of its own);
        always with its UI, and only while the game is the window in front. Scaled like an engine screenshot.
        Returns the path."""
        return self.window_screenshot(out)

    def burst(self, *args, **kwargs) -> str:
        raise GameError("bursts need the engine's own screenshots (UE4SS); with CET take single shots")

    def console(self, command: str, mod: str | None = None) -> str:
        raise GameError("console commands are an Unreal Engine thing (UE4SS runs them); CET has none — run Lua instead")

    def crash_report(self, journal_count: int = 6) -> str:
        """Everything that says why the game died: is it running, REDEngine's newest crash report (its stack trace,
        the head of report.txt and the minidump's exception), the end of CET's scripting.log, and the last requests
        cetmod sent."""
        now = time.time()
        out: list[str] = []
        st = self.status()
        out.append("game: " + ("running" if st.running else "not running" if st.running is False else "unknown")
                   + (f" ({st.process}{', started ' + st.started if st.started else ''})" if st.running else ""))
        folders = self.transport.crash_files().get("folders") or []
        if folders:
            f = folders[0]
            path = f["path"].rstrip("/\\")
            m = max(0, int((now - f["time"]) / 60))
            when = f"{m} min ago" if m < 120 else time.strftime("%Y-%m-%d %H:%M", time.localtime(f["time"]))
            if st.running:
                out.append("(the game is running now: the crash report below is from an earlier session)")
            out.append(f"\nnewest REDEngine crash report ({when}): {path}")
            names = f.get("files") or []
            for name, head in (("stacktrace.txt", 40), ("report.txt", 25)):
                if name in names:
                    try:
                        lines = self.transport.read_text(f"{path}/{name}").splitlines()
                        out.append(f"{name}:")
                        out += ["  " + l for l in lines[:head] if l.strip()]
                    except Exception as e:
                        out.append(f"{name}: could not read it: {e}")
            dmp = next((n for n in names if n.lower().endswith(".dmp")), None)
            if dmp:
                tmp = tempfile.mkdtemp(prefix="cetmod-crash-")
                local = f"{tmp}/{dmp}"
                try:
                    self.transport.fetch(f"{path}/{dmp}", local)
                    with open(local, "rb") as fh:
                        out += parse_minidump(fh.read(), INTERESTING).describe()[:16]
                except Exception as e:
                    out.append(f"minidump: could not read {path}/{dmp}: {e}")
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        else:
            out.append("\nno REDEngine crash report")
        out.append(f"\n{self.transport.engine.log}, last lines:")
        out += ["  " + l for l in self.log(25)]
        entries = journal.tail(self.settings, journal_count)
        out.append("\nlast requests cetmod sent (a request with no answer right before the crash is the first suspect):")
        out += ["  " + l for l in journal.render(entries).splitlines()]
        if entries and st.running is False and entries[-1].get("done") in (None, "timeout", "failed"):
            out.append("  -> the last request got no answer and the game is gone: it most likely killed the game.")
        return "\n".join(out)
