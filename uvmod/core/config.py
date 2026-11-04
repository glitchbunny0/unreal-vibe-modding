"""Settings: environment variables, else ~/.config/<app.name>/config (KEY=value lines, parsed, never executed).
Which app is being configured comes in as an App, so nothing here names a mod loader."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .app import App

# Settings every app has, by suffix; an app adds its root key first and its extra keys (App.keys) before JOURNAL.
KEYS = {
    "TRANSPORT": "local | ssh (default: ssh when UVMOD_HOST is set, else local)",
    "HOST": "ssh target for the ssh transport, e.g. player@192.168.1.20",
    "MOD": "the Lua mod whose state runs code (default: the only one there is; see: uvmod mods)",
    "LIMIT_MS": "time limit per request in ms, 1-5000 (default: the game's, 250)",
    "TIMEOUT": "seconds to wait for an answer (default 10)",
    "PROCESS": "the game's process name (default: the *-Shipping.exe beside the ue4ss folder)",
    "SHOTS": "screenshot folder (default: <LocalAppData>/<Project>/Saved/Screenshots/Windows)",
    "APPID": "the game's Steam app id, for `launch` (default: read from the Steam library the game is in)",
    "LAUNCH": "a command that starts the game, for `launch` instead of Steam (run on the game's machine)",
    "JOURNAL": "where uvmod records the requests it sends, for `crash` (default: ~/.local/state/uvmod/journal.jsonl; off: none)",
}


def keys_for(app: App) -> dict[str, str]:
    """The full settings of one app, <PREFIX>_KEY -> help, for the help output: the root key first, then the shared
    keys with the app's extra keys in their place (before JOURNAL), which is the order the help has always shown."""
    out = {f"{app.prefix}_{app.root_key}": app.root_help}
    for suffix, help_text in KEYS.items():
        if suffix == "JOURNAL":
            out.update({f"{app.prefix}_{s}": h for s, h in app.keys.items()})
        out[f"{app.prefix}_{suffix}"] = help_text
    return out


def config_path(app: App) -> Path:
    if f"{app.prefix}_CONFIG" in os.environ:
        return Path(os.environ[f"{app.prefix}_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / app.name / "config"


def _read_file(path: Path, prefix: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    line = re.compile(rf"^\s*({prefix}_[A-Z0-9_]+)\s*=\s*(.*?)\s*$")
    for text_line in text.splitlines():
        m = line.match(text_line)
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
    app: App
    root: str  # the mod loader's folder: what <PREFIX>_<app.root_key> names
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
    extra: dict = field(default_factory=dict)  # app-specific settings by suffix (App.keys)

    @property
    def engine(self):
        return self.app.engine

    @classmethod
    def load(cls, app: App, overrides: dict[str, str] | None = None) -> "Settings":
        prefix = app.prefix
        values = _read_file(config_path(app), prefix)
        values.update({k: v for k, v in os.environ.items() if k.startswith(prefix + "_")})
        values.update(overrides or {})
        root = values.get(f"{prefix}_{app.root_key}", "")
        if not root:
            raise ConfigError(f"{prefix}_{app.root_key} is not set (environment or {config_path(app)}; see: {app.name} help)")
        host = values.get(f"{prefix}_HOST", "")
        transport = values.get(f"{prefix}_TRANSPORT") or ("ssh" if host else "local")
        if transport not in ("local", "ssh"):
            raise ConfigError(f"unknown transport '{transport}' (have: local ssh)")
        limit = values.get(f"{prefix}_LIMIT_MS")
        try:
            limit_ms = int(limit) if limit else None
            timeout = float(values.get(f"{prefix}_TIMEOUT") or 10)
        except ValueError as e:
            raise ConfigError(f"bad number in settings: {e}") from None
        return cls(
            app=app,
            root=root,
            transport=transport,
            host=host,
            mod=values.get(f"{prefix}_MOD", ""),
            limit_ms=limit_ms,
            timeout=timeout,
            process=values.get(f"{prefix}_PROCESS", ""),
            shots=values.get(f"{prefix}_SHOTS", ""),
            appid=values.get(f"{prefix}_APPID", ""),
            launch=values.get(f"{prefix}_LAUNCH", ""),
            journal=values.get(f"{prefix}_JOURNAL", ""),
            extra={suffix: values.get(f"{prefix}_{suffix}", "") for suffix in app.keys},
        )
