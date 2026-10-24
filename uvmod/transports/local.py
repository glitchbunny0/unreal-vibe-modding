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


def put_if_absent(path: Path, data: bytes, tag: str) -> bool:
    """Publish a file under `path` only if nothing is there: False if another writer got there first.
    `tag` keeps concurrent writers' temporary files apart."""
    tmp = path.with_name(f"{path.name}.{tag}.tmp")
    tmp.write_bytes(data)
    try:
        if os.name == "nt":
            os.rename(tmp, path)  # Windows: fails when the target exists
            return True
        try:
            os.link(tmp, path)  # POSIX rename would replace the target; a hard link fails instead
        except FileExistsError:
            raise
        except OSError:  # no hard links here (some network shares): check, then rename
            if path.exists():
                return False
            os.replace(tmp, path)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


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
            # One slot: wait until it is free, and publish only into a free slot, so concurrent callers
            # take turns instead of replacing each other's requests.
            while not put_if_absent(target, slot, request_id):
                if time.monotonic() >= deadline:
                    return Exchange(delivery, None)
                time.sleep(POLL)
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
        return self._saved_dir("Screenshots/Windows")

    def _saved_dir(self, sub: str) -> Path | None:
        """<LocalAppData>/<Project>/Saved/<sub>: on Windows, or in the game's Proton prefix."""
        project = re.sub(r"-(Win64|WinGDK)-Shipping$", "", self._process())
        if not project:
            return None
        rel = Path(project) / "Saved" / sub
        if os.environ.get("LOCALAPPDATA") and (Path(os.environ["LOCALAPPDATA"]) / rel).is_dir():
            return Path(os.environ["LOCALAPPDATA"]) / rel
        parts = self.root.as_posix().split("/steamapps/")
        if len(parts) > 1:
            pattern = f"{parts[0]}/steamapps/compatdata/*/pfx/drive_c/users/steamuser/AppData/Local/{rel.as_posix()}"
            for d in glob.glob(pattern):
                return Path(d)
        return None

    # --- starting and stopping the game ---------------------------------------------------------------------------

    def appid(self) -> str:
        """UVMOD_APPID, else the Steam library's appmanifest whose installdir holds this game."""
        if self.settings.appid:
            return self.settings.appid
        parts = self.root.as_posix().split("/steamapps/common/")
        if len(parts) < 2:
            return ""
        installdir = parts[1].split("/")[0]
        for manifest in glob.glob(f"{parts[0]}/steamapps/appmanifest_*.acf"):
            try:
                text = Path(manifest).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if re.search(r'"installdir"\s+"' + re.escape(installdir) + '"', text, re.I):
                m = re.search(r'"appid"\s+"(\d+)"', text)
                if m:
                    return m.group(1)
        return ""

    def launch(self) -> str:
        if self.settings.launch:
            subprocess.Popen(self.settings.launch, shell=True, start_new_session=os.name != "nt",
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return f"started: {self.settings.launch}"
        appid = self.appid()
        if not appid:
            raise TransportError("no Steam app id: set UVMOD_APPID (or UVMOD_LAUNCH to a command that starts the game)")
        url = f"steam://rungameid/{appid}"
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]  # Windows only
        else:
            opener = shutil.which("steam") or shutil.which("xdg-open")
            if not opener:
                raise TransportError("neither steam nor xdg-open is on PATH; set UVMOD_LAUNCH")
            subprocess.Popen([opener, url], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"asked Steam to start app {appid}"

    def stop(self) -> str:
        process = self._process()
        if not process:
            raise TransportError("unknown game process (set UVMOD_PROCESS)")
        if self._pid(process) is None:
            return f"{process} is not running"
        if os.name == "nt":
            subprocess.run(["taskkill", "/IM", f"{process}.exe", "/F"], capture_output=True, timeout=30)
            subprocess.run(["taskkill", "/IM", "CrashReportClient.exe", "/F"], capture_output=True, timeout=30)
        else:
            subprocess.run(["pkill", "-f", "--", process], capture_output=True, timeout=30)
        for _ in range(50):
            if self._pid(process) is None:
                return f"stopped {process}"
            time.sleep(0.2)
        return f"{process} is still running"

    # --- crashes --------------------------------------------------------------------------------------------------

    def crash_files(self) -> dict:
        out: dict = {"folders": [], "dumps": []}
        crashes = self._saved_dir("Crashes")
        if crashes and crashes.is_dir():
            folders = sorted((p for p in crashes.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
            out["folders"] = [{"path": str(p), "time": p.stat().st_mtime, "files": sorted(f.name for f in p.iterdir())}
                              for p in folders[:3]]
        dumps = sorted(self.root.glob("crash_*.dmp"), key=lambda p: p.stat().st_mtime, reverse=True)
        out["dumps"] = [{"path": str(p), "time": p.stat().st_mtime} for p in dumps[:3]]
        return out

    def read_text(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8", errors="replace")

    def files(self, pattern: str) -> list[dict]:
        found = sorted(self.root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"path": str(p), "time": p.stat().st_mtime, "size": p.stat().st_size} for p in found if p.is_file()]

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
