"""What differs between the mod loaders the core drives: where their files are, what the game's process is called.
An app defines its own Engine; `ue4ss` here is the one uvmod uses (the root is the ue4ss folder)."""

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
