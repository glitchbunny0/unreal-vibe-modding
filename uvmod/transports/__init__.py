"""Transports reach the game's ue4ss folder: `local` (a path on this machine) or `ssh` (Windows over SSH)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from ..config import Settings


@dataclass
class Exchange:
    """One request's round trip. `answer` is None if nothing came back in time."""

    delivery: str  # "native" (UE4SS) | "bridge" (VibeBridge) | "missing" (no LuaRepl folder)
    answer: str | None


@dataclass
class Status:
    process: str = ""
    running: bool | None = None
    pid: int | None = None
    started: str = ""
    log_time: str = ""
    log_lines: list[str] = field(default_factory=list)
    repl_setting: str = ""
    hot_reload_setting: str = ""
    repl_folder: bool = False
    bridge_age: float | None = None
    bridge_installed: bool = False


class Transport:
    def __init__(self, settings: Settings):
        self.settings = settings

    def exchange(self, request_id: str, native: bytes, slot: bytes, timeout: float) -> Exchange:
        raise NotImplementedError

    def status(self, status_pattern: str) -> Status:
        raise NotImplementedError

    def log(self, lines: int, pattern: str = "") -> list[str]:
        raise NotImplementedError

    def follow(self, pattern: str = "") -> Iterator[str]:
        raise NotImplementedError

    def push(self, local_dir: str) -> str:
        raise NotImplementedError

    def swap(self, local_dir: str, swap_id: str, timeout: float) -> str | None:
        raise NotImplementedError

    def newest_shot(self) -> tuple[str, str] | None:
        raise NotImplementedError

    def recent_shots(self, count: int) -> list[str]:
        """The paths of the newest `count` screenshots, newest first."""
        raise NotImplementedError

    def fetch(self, path: str, dest: str) -> None:
        raise NotImplementedError

    def fetch_large(self, path: str, dest: str) -> None:
        """Like fetch, for a file of a hundred MB or more (transports may compress it on the way)."""
        self.fetch(path, dest)

    def files(self, pattern: str) -> list[dict]:
        """Files in the ue4ss folder matching a glob, newest first: [{"path", "time", "size"}]."""
        raise NotImplementedError

    def make_folder(self, name: str) -> None:
        """Create a folder inside the ue4ss folder (the LuaRepl folder for VibeBridge)."""
        raise NotImplementedError

    def launch(self) -> str:
        """Start the game (UVMOD_LAUNCH, else Steam with UVMOD_APPID or the app id found in the Steam library)."""
        raise NotImplementedError

    def stop(self) -> str:
        """End the game's process (and Unreal's crash reporter)."""
        raise NotImplementedError

    def crash_files(self) -> dict:
        """The newest crash evidence: {"folders": [{"path", "time", "files"}], "dumps": [{"path", "time"}]}, newest first.
        Folders are Unreal's Saved/Crashes/<id>; dumps are UE4SS's own crash_*.dmp. Times are Unix seconds."""
        raise NotImplementedError

    def read_text(self, path: str) -> str:
        raise NotImplementedError

    def powershell(self, script: str) -> str:
        raise TransportError("this needs the ssh transport (a Windows box reached over SSH)")


class TransportError(Exception):
    pass


def make(settings: Settings) -> Transport:
    if settings.transport == "ssh":
        from .ssh import SshTransport

        return SshTransport(settings)
    from .local import LocalTransport

    return LocalTransport(settings)
