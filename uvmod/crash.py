"""Crash triage without extra tools: read an Unreal crash folder's CrashContext.runtime-xml. (The Windows minidump
reader lives in uvmod.core.minidump.) Standard library only."""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree import ElementTree


@dataclass
class CrashContext:
    error: str = ""
    engine: str = ""
    callstack: list[str] = field(default_factory=list)
    has_ue4ss: bool | None = None


def parse_crash_context(text: str) -> CrashContext:
    """CrashContext.runtime-xml from an Unreal crash folder."""
    ctx = CrashContext()
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return ctx
    def get(tag: str) -> str:
        el = root.find(f".//{tag}")
        return (el.text or "").strip() if el is not None else ""
    ctx.error = get("ErrorMessage")
    ctx.engine = get("EngineVersion")
    ctx.callstack = [l.strip() for l in get("CallStack").splitlines() if l.strip()][:40]
    modules = get("Modules")
    if modules:
        ctx.has_ue4ss = "ue4ss.dll" in modules.lower()
    return ctx
