"""cetmod: drive a running Cyberpunk 2077 through Cyber Engine Tweaks and the VibeBridge CET mod, on the same core
as uvmod (everything here imports only uvmod.core and itself)."""

from __future__ import annotations

from uvmod.core import CORE_API
from uvmod.core.app import App
from uvmod.core.engine import Engine

if CORE_API != 1:
    raise ImportError(f"cetmod needs uvmod.core with CORE_API 1, found {CORE_API}: update uvmod or cetmod")

__version__ = "0.1.0.dev0"

CET = Engine(
    name="cet",
    label="Cyber Engine Tweaks",
    repl="mods/VibeBridge/LuaRepl",
    mods="mods",
    log="scripting.log",
    bridge_marker="mods/VibeBridge/init.lua",
    reporter="REDEngineErrorReporter",
    process="Cyberpunk2077",
    exe=("..", "..", "Cyberpunk2077.exe"),  # the CET folder is <game>/bin/x64/plugins/cyber_engine_tweaks: two up is the game's exe
    crash_dir="localappdata:REDEngine/ReportQueue",
    status_pattern=r"\[VibeBridge\]|Mod .* (loaded|failed)|Ignoring mod|LuaVM: Reloaded|Tried to access invalid",
)

APP = App(
    name="cetmod",
    prefix="CETMOD",
    root_key="CET",
    root_help="Cyberpunk 2077: the Cyber Engine Tweaks folder (<game>/bin/x64/plugins/cyber_engine_tweaks)",
    engine=CET,
    keys={},
)
