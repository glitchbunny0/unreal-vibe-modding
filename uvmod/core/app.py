"""One app's identity: the name its config and journal folders live under, the settings prefix, and the mod loader —
and so the engine — it drives."""

from __future__ import annotations

from dataclasses import dataclass

from .engine import Engine


@dataclass(frozen=True)
class App:
    name: str        # "uvmod": config ~/.config/<name>/config, journal ~/.local/state/<name>/journal.jsonl
    prefix: str      # "UVMOD": settings are <PREFIX>_<KEY>; <PREFIX>_CONFIG overrides the config file path
    root_key: str    # "UE4SS": <PREFIX>_<root_key> names the mod loader's folder
    root_help: str   # its line in the settings help
    engine: Engine
    keys: dict       # app-specific extra keys, suffix -> help (e.g. {"INDEX": "..."}); read into Settings.extra
