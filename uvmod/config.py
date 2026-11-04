"""The app uvmod drives: UE4SS in an Unreal Engine game; plus the Settings naming its folder as uvmod always has."""

from __future__ import annotations

from .core.app import App
from .core.config import ConfigError, Settings as CoreSettings  # ConfigError re-exported: uvmod's callers import it here
from .core.engine import UE4SS

__all__ = ["ConfigError", "Settings", "UE4SS_APP"]

_INDEX_HELP = "the reflection index `api` and `check` read (default: the newest one built for this game)"

UE4SS_APP = App(
    name="uvmod",
    prefix="UVMOD",
    root_key="UE4SS",
    root_help="the game's ue4ss folder: a path on this machine, or the Windows path with the ssh transport",
    engine=UE4SS,
    keys={"INDEX": _INDEX_HELP},
)


class Settings(CoreSettings):
    """Settings for uvmod: UVMOD_UE4SS names the game's ue4ss folder."""

    @classmethod
    def load(cls, overrides: dict[str, str] | None = None) -> "Settings":
        return super().load(UE4SS_APP, overrides)

    @property
    def index(self) -> str:
        return self.extra.get("INDEX", "")
