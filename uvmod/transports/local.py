"""local transport: the game's ue4ss folder is a path on this machine (Windows, Linux with Proton, Steam Deck,
WSL, a mounted share)."""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .. import protocol
from . import Exchange, Status, Transport, TransportError

POLL = 0.01


def put_atomic(path: Path, data: bytes) -> None:
    """Write next to the final name and rename into place, so a reader never sees a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def read_ini_line(ini: Path, key: str) -> str:
    try:
        text = ini.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    found = ""
    for line in text.splitlines():
        if re.match(rf"^\s*{re.escape(key)}\s*=", line):
            found = line.strip()
    return found


class LocalTransport(Transport):
    def __init__(self, settings):
        super().__init__(settings)
        self.root = Path(settings.ue4ss)
        if not self.root.is_dir():
            raise TransportError(f"UVMOD_UE4SS is not a folder on this machine: {self.root} (local transport)")
        self.folder = self.root / protocol.FOLDER

    # --- requests ------------------------------------------------------------------------------------------------

    def bridge_age(self) -> float | None:
        try:
            return max(0.0, time.time() - (self.folder / protocol.BRIDGE_INFO).stat().st_mtime)
        except OSError:
            return None

    def exchange(self, request_id: str, native: bytes, slot: bytes, timeout: float) -> Exchange:
        if not self.folder.is_dir():
            return Exchange("missing", None)
        deadline = time.monotonic() + timeout
        answer = self.folder / f"{request_id}.response.json"
        age = self.bridge_age()
        if age is not None and age <= protocol.BRIDGE_FRESH_SECONDS:
            delivery, target = "bridge", self.folder / protocol.BRIDGE_SLOT
            # One slot: wait until the previous request has been taken.
            while target.exists() and time.monotonic() < deadline:
                time.sleep(POLL)
            if target.exists():
                return Exchange(delivery, None)
            put_atomic(target, slot)
        else:
            delivery, target = "native", self.folder / f"{request_id}.request.json"
            put_atomic(target, native)
        while time.monotonic() < deadline:
            if answer.exists():
                try:
                    text = answer.read_text(encoding="utf-8")
                except OSError:
                    time.sleep(POLL)
                    continue
                answer.unlink(missing_ok=True)
                return Exchange(delivery, text)
            time.sleep(POLL)
        self._withdraw(target, request_id)
        return Exchange(delivery, None)

    @staticmethod
    def _withdraw(target: Path, request_id: str) -> None:
        """Take back a request nobody picked up; in the bridge slot only if it is still ours."""
        try:
            if target.name == protocol.BRIDGE_SLOT:
                if json.loads(target.read_text(encoding="utf-8")).get("id") != request_id:
                    return
            target.unlink()
        except (OSError, ValueError):
            pass

    # --- the game and its files --------------------------------------------------------------------------------

    def _process(self) -> str:
        if self.settings.process:
            return self.settings.process
        for exe in sorted(glob.glob(str(self.root.parent / "*-Shipping.exe")) + glob.glob(str(self.root / "*-Shipping.exe"))):
            return Path(exe).stem
        return ""

    def _pid(self, process: str) -> int | None:
        if not process:
            return None
        try:
            if os.name == "nt":
                out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {process}.exe", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=10).stdout
                m = re.search(r'^"[^"]+","(\d+)"', out, re.M)
                return int(m.group(1)) if m else None
            out = subprocess.run(["pgrep", "-o", "-f", "--", process], capture_output=True, text=True, timeout=10).stdout
            return int(out.split()[0]) if out.split() else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def status(self, status_pattern: str) -> Status:
        s = Status(process=self._process())
        s.pid = self._pid(s.process)
        s.running = (s.pid is not None) if s.process else None
        log = self.root / "UE4SS.log"
        if log.exists():
            s.log_time = datetime.fromtimestamp(log.stat().st_mtime).strftime("%H:%M:%S")
            s.log_lines = [l for l in self.log(20000) if re.search(status_pattern, l)][-6:]
        ini = self.root / "UE4SS-settings.ini"
        s.repl_setting = read_ini_line(ini, "ExternalLuaReplEnabled")
        s.hot_reload_setting = read_ini_line(ini, "EnableHotReloadSystem")
        s.repl_folder = self.folder.is_dir()
        s.bridge_age = self.bridge_age()
        s.bridge_installed = (self.root / "Mods" / "VibeBridge" / "enabled.txt").exists()
        return s

    def log(self, lines: int, pattern: str = "") -> list[str]:
        try:
            text = (self.root / "UE4SS.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        all_lines = [l.rstrip("\r") for l in text.splitlines()]
        if pattern:
            rx = re.compile(pattern, re.I)
            all_lines = [l for l in all_lines[-20000:] if rx.search(l)]
        return all_lines[-lines:] if lines > 0 else []

    def follow(self, pattern: str = "") -> Iterator[str]:
        path = self.root / "UE4SS.log"
        rx = re.compile(pattern or ".", re.I)
        pos = path.stat().st_size if path.exists() else 0
        pending = ""
        while True:
            try:
                size = path.stat().st_size
                if size < pos:  # the game restarted and the log was rewritten
                    pos = 0
                if size > pos:
                    with path.open("rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                    pos += len(chunk)
                    pending += chunk.decode("utf-8", errors="replace")
                    *complete, pending = pending.split("\n")
                    for line in complete:
                        line = line.rstrip("\r")
                        if rx.search(line):
                            yield line
            except OSError:
                pass
            time.sleep(0.2)

    def _copy_mod(self, local_dir: str) -> Path:
        src = Path(local_dir)
        dest = self.root / "Mods" / src.name
        shutil.copytree(src, dest, dirs_exist_ok=True, ignore=lambda d, names: ["hotswap"] if Path(d) == src else [])
        return dest

    def push(self, local_dir: str) -> str:
        dest = self._copy_mod(local_dir)
        count = sum(1 for p in dest.rglob("*") if p.is_file())
        return f"pushed {dest.name} -> {dest} ({count} files)"

    def swap(self, local_dir: str, swap_id: str, timeout: float) -> str | None:
        dest = self._copy_mod(local_dir)
        hotswap = dest / "hotswap"
        hotswap.mkdir(exist_ok=True)
        req, out = hotswap / "req", hotswap / f"{swap_id}.out"
        put_atomic(req, swap_id.encode("ascii"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if out.exists():
                text = out.read_text(encoding="utf-8", errors="replace")
                out.unlink(missing_ok=True)
                return text
            time.sleep(POLL)
        req.unlink(missing_ok=True)
        return None

    def _shots_dir(self) -> Path | None:
        if self.settings.shots:
            return Path(self.settings.shots)
        project = re.sub(r"-(Win64|WinGDK)-Shipping$", "", self._process())
        if not project:
            return None
        rel = Path(project) / "Saved" / "Screenshots" / "Windows"
        if os.environ.get("LOCALAPPDATA") and (Path(os.environ["LOCALAPPDATA"]) / rel).is_dir():
            return Path(os.environ["LOCALAPPDATA"]) / rel
        parts = self.root.as_posix().split("/steamapps/")
        if len(parts) > 1:
            pattern = f"{parts[0]}/steamapps/compatdata/*/pfx/drive_c/users/steamuser/AppData/Local/{rel.as_posix()}"
            for d in glob.glob(pattern):
                return Path(d)
        return None

    def newest_shot(self) -> tuple[str, str] | None:
        d = self._shots_dir()
        if not d or not d.is_dir():
            return None
        shots = sorted(d.glob("*.png"), key=lambda p: p.stat().st_mtime)
        return (str(shots[-1]), str(shots[-1].stat().st_mtime)) if shots else None

    def make_folder(self, name: str) -> None:
        (self.root / name).mkdir(exist_ok=True)

    def fetch(self, path: str, dest: str) -> None:
        shutil.copyfile(path, dest)
