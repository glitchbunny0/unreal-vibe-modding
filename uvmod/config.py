"""Settings: environment variables, else ~/.config/uvmod/config (KEY=value lines, parsed, never executed)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

KEYS = {
    "UVMOD_UE4SS": "the game's ue4ss folder: a path on this machine, or the Windows path with the ssh transport",
    "UVMOD_TRANSPORT": "local | ssh (default: ssh when UVMOD_HOST is set, else local)",
    "UVMOD_HOST": "ssh target for the ssh transport, e.g. player@192.168.1.20",
    "UVMOD_MOD": "the Lua mod whose state runs code (default: the only one there is; see: uvmod mods)",
    "UVMOD_LIMIT_MS": "time limit per request in ms, 1-5000 (default: the game's, 250)",
    "UVMOD_TIMEOUT": "seconds to wait for an answer (default 10)",
    "UVMOD_PROCESS": "the game's process name (default: the *-Shipping.exe beside the ue4ss folder)",
    "UVMOD_SHOTS": "screenshot folder (default: <LocalAppData>/<Project>/Saved/Screenshots/Windows)",
    "UVMOD_APPID": "the game's Steam app id, for `launch` (default: read from the Steam library the game is in)",
    "UVMOD_LAUNCH": "a command that starts the game, for `launch` instead of Steam (run on the game's machine)",
    "UVMOD_JOURNAL": "where uvmod records the requests it sends, for `crash` (default: ~/.local/state/uvmod/journal.jsonl; off: none)",
}

_LINE = re.compile(r"^\s*(UVMOD_[A-Z0-9_]+)\s*=\s*(.*?)\s*$")


def config_path() -> Path:
    if "UVMOD_CONFIG" in os.environ:
        return Path(os.environ["UVMOD_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "uvmod" / "config"


def _read_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        value = m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[m.group(1)] = value
    return values


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    ue4ss: str
    transport: str
    host: str = ""
    mod: str = ""
    limit_ms: int | None = None
    timeout: float = 10.0
    process: str = ""
    shots: str = ""
    appid: str = ""
    launch: str = ""
    journal: str = ""

    @classmethod
    def load(cls, overrides: dict[str, str] | None = None) -> "Settings":
        values = _read_file(config_path())
        values.update({k: v for k, v in os.environ.items() if k.startswith("UVMOD_")})
        values.update(overrides or {})
        ue4ss = values.get("UVMOD_UE4SS", "")
        if not ue4ss:
            raise ConfigError(f"UVMOD_UE4SS is not set (environment or {config_path()}; see: uvmod help)")
        host = values.get("UVMOD_HOST", "")
        transport = values.get("UVMOD_TRANSPORT") or ("ssh" if host else "local")
        if transport not in ("local", "ssh"):
            raise ConfigError(f"unknown transport '{transport}' (have: local ssh)")
        limit = values.get("UVMOD_LIMIT_MS")
        try:
            limit_ms = int(limit) if limit else None
            timeout = float(values.get("UVMOD_TIMEOUT") or 10)
        except ValueError as e:
            raise ConfigError(f"bad number in settings: {e}") from None
        return cls(
            ue4ss=ue4ss,
            transport=transport,
            host=host,
            mod=values.get("UVMOD_MOD", ""),
            limit_ms=limit_ms,
            timeout=timeout,
            process=values.get("UVMOD_PROCESS", ""),
            shots=values.get("UVMOD_SHOTS", ""),
            appid=values.get("UVMOD_APPID", ""),
            launch=values.get("UVMOD_LAUNCH", ""),
            journal=values.get("UVMOD_JOURNAL", ""),
        )
