"""What differs between the mod loaders uvmod drives: where their files are, what the game's process is called.

`ue4ss` is UE4SS in an Unreal Engine game (the root is the ue4ss folder). `cet` is Cyber Engine Tweaks in Cyberpunk 2077
(the root is CET's folder, <game>/bin/x64/plugins/cyber_engine_tweaks); there VibeBridge is a CET mod, and since CET
lets a mod write only inside its own folder, the LuaRepl folder lives in the mod.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Engine:
    name: str
    label: str
    repl: str  # the LuaRepl folder, relative to the root
    mods: str  # where mod folders go, relative to the root
    log: str  # the log `log`, `follow` and `status` read, relative to the root
    bridge_marker: str  # exists when VibeBridge is installed, relative to the root
    reporter: str  # the engine's crash reporter process, closed by `stop`
    process: str = ""  # the game's process name when it is fixed ("" = find it)
    exe: tuple = ()  # the game's exe relative to the root, which `launch` starts directly (empty: Steam only)
    crash_dir: str = ""  # where crash folders are: "saved:<sub>" under Saved, "localappdata:<sub>" under LocalAppData
    status_pattern: str = ""  # the log lines `status` shows, valid in Python and .NET regular expressions


UE4SS = Engine(
    name="ue4ss",
    label="UE4SS",
    repl="LuaRepl",
    mods="Mods",
    log="UE4SS.log",
    bridge_marker="Mods/VibeBridge/enabled.txt",
    reporter="CrashReportClient",
    exe=(),  # Steam starts the game
    crash_dir="saved:Crashes",
    status_pattern=r"UE4SS - v|\[ExternalLuaRepl\] (?!Request )|\[VibeBridge\]|loaded WITHOUT logic|swapped logic|swap REFUSED",
)

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

ENGINES = {e.name: e for e in (UE4SS, CET)}


def get(name: str) -> Engine:
    return ENGINES[name]
