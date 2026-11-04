"""Settings for cetmod: CETMOD_CET names the Cyber Engine Tweaks folder; everything else is core's generic set."""

from __future__ import annotations

from . import APP
from uvmod.core.config import Settings as CoreSettings


class Settings(CoreSettings):
    """Settings for one Cyberpunk 2077 install reached through CET."""

    @classmethod
    def load(cls, overrides: dict[str, str] | None = None) -> "Settings":
        return super().load(APP, overrides)
