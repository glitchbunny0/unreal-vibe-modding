"""The apps uvmod drives today: UE4SS in an Unreal Engine game, and — until it moves out for good — Cyber Engine
Tweaks; plus the Settings that pick between them exactly as uvmod always has."""

from __future__ import annotations

import os

from .core.app import App
from .core.config import ConfigError, Settings as CoreSettings, _read_file, config_path
from .core.engine import CET, UE4SS

_INDEX_HELP = "the reflection index `api` and `check` read (default: the newest one built for this game)"

UE4SS_APP = App(
    name="uvmod",
    prefix="UVMOD",
    root_key="UE4SS",
    root_help="the game's ue4ss folder: a path on this machine, or the Windows path with the ssh transport",
    engine=UE4SS,
    keys={"INDEX": _INDEX_HELP},
)

CET_APP = App(
    name="uvmod",
    prefix="UVMOD",
    root_key="CET",
    root_help="Cyberpunk 2077 instead: the Cyber Engine Tweaks folder (<game>/bin/x64/plugins/cyber_engine_tweaks)",
    engine=CET,
    keys={"INDEX": _INDEX_HELP},
)


class Settings(CoreSettings):
    """Settings for uvmod as it is today: exactly one of UVMOD_UE4SS and UVMOD_CET names the mod loader's folder."""

    @classmethod
    def load(cls, overrides: dict[str, str] | None = None) -> "Settings":
        values = _read_file(config_path(UE4SS_APP), "UVMOD")
        values.update({k: v for k, v in os.environ.items() if k.startswith("UVMOD_")})
        values.update(overrides or {})
        ue4ss, cet = values.get("UVMOD_UE4SS", ""), values.get("UVMOD_CET", "")
        if ue4ss and cet:
            raise ConfigError("both UVMOD_UE4SS and UVMOD_CET are set; keep one (UVMOD_CONFIG can point at a config file per game)")
        if not ue4ss and not cet:
            raise ConfigError(f"neither UVMOD_UE4SS nor UVMOD_CET is set (environment or {config_path(UE4SS_APP)}; see: uvmod help)")
        return super().load(CET_APP if cet else UE4SS_APP, values)

    @property
    def is_cet(self) -> bool:
        return self.app is CET_APP

    @property
    def index(self) -> str:
        return self.extra.get("INDEX", "")
