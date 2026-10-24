"""Crash triage without extra tools: read a Windows minidump (exception, faulting module, return addresses found on
the crashing stack) and an Unreal crash folder's CrashContext.runtime-xml. Standard library only."""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from xml.etree import ElementTree

EXCEPTIONS = {
    0xC0000005: "access violation",
    0xC00000FD: "stack overflow",
    0xC0000094: "integer divide by zero",
    0xC000001D: "illegal instruction",
    0xC0000409: "stack buffer overrun / fail-fast",
    0xC0000374: "heap corruption",
    0x80000003: "breakpoint",
    0xE06D7363: "C++ exception",
}

STREAM_THREADS, STREAM_MODULES, STREAM_MEMORY, STREAM_EXCEPTION, STREAM_MEMORY64 = 3, 4, 5, 6, 9


@dataclass
class Module:
    name: str
    base: int
    size: int

    @property
    def short(self) -> str:
        return re.split(r"[\\/]", self.name)[-1]


@dataclass
class Minidump:
    code: int | None = None
    address: int | None = None
    info: list[int] = field(default_factory=list)
    modules: list[Module] = field(default_factory=list)
    stack: list[tuple[str, int]] = field(default_factory=list)  # (module, RVA) of return-address candidates
    rip: int | None = None
    rsp: int | None = None
    note: str = ""

    def where(self, address: int | None) -> tuple[str, int] | None:
        if address is None:
            return None
        for m in self.modules:
            if m.base <= address < m.base + m.size:
                return m.short, address - m.base
        return None

    def describe(self) -> list[str]:
        if self.code is None:
            return [f"minidump: no exception record{' (' + self.note + ')' if self.note else ''}"]
        name = EXCEPTIONS.get(self.code, "")
        place = self.where(self.address)
        lines = [f"exception 0x{self.code:08X}{' (' + name + ')' if name else ''} at "
                 + (f"{place[0]}+0x{place[1]:X}" if place else f"0x{self.address or 0:X} (outside every module)")]
        if self.code == 0xC0000005 and len(self.info) >= 2:
            lines.append(f"  {'writing' if self.info[0] == 1 else 'executing' if self.info[0] == 8 else 'reading'} "
                         f"address 0x{self.info[1]:X}" + ("  <- a null pointer" if self.info[1] < 0x10000 else ""))
        if self.stack:
            lines.append("return addresses on the crashing stack (a scan: the first ones are the most reliable):")
            lines += [f"  {mod}+0x{rva:X}" for mod, rva in self.stack]
        if self.note:
            lines.append(f"  ({self.note})")
        return lines


def _string(data: bytes, rva: int) -> str:
    (length,) = struct.unpack_from("<I", data, rva)
    return data[rva + 4: rva + 4 + length].decode("utf-16-le", errors="replace")


def parse_minidump(data: bytes, interesting: tuple[str, ...] = ("ue4ss.dll", "shipping.exe", "main.dll"),
                   max_frames: int = 30) -> Minidump:
    """`interesting`: substrings of module names whose addresses count as stack frames (case-insensitive)."""
    dump = Minidump()
    if len(data) < 32 or data[:4] != b"MDMP":
        dump.note = "not a minidump"
        return dump
    _, _, count, dir_rva = struct.unpack_from("<4sIII", data, 0)
    streams: dict[int, tuple[int, int]] = {}
    for i in range(count):
        kind, size, rva = struct.unpack_from("<III", data, dir_rva + 12 * i)
        streams.setdefault(kind, (size, rva))

    if STREAM_MODULES in streams:
        _, rva = streams[STREAM_MODULES]
        (n,) = struct.unpack_from("<I", data, rva)
        for i in range(n):
            base, size, _, _, name_rva = struct.unpack_from("<QIIII", data, rva + 4 + 108 * i)
            dump.modules.append(Module(_string(data, name_rva), base, size))
        dump.modules.sort(key=lambda m: m.base)

    ranges: list[tuple[int, int, int]] = []  # (start, size, file offset)
    if STREAM_MEMORY in streams:
        _, rva = streams[STREAM_MEMORY]
        (n,) = struct.unpack_from("<I", data, rva)
        for i in range(n):
            start, size, mrva = struct.unpack_from("<QII", data, rva + 4 + 16 * i)
            ranges.append((start, size, mrva))
    if STREAM_MEMORY64 in streams:
        _, rva = streams[STREAM_MEMORY64]
        n, base_rva = struct.unpack_from("<QQ", data, rva)
        offset = base_rva
        for i in range(n):
            start, size = struct.unpack_from("<QQ", data, rva + 16 + 16 * i)
            ranges.append((start, size, offset))
            offset += size

    if STREAM_EXCEPTION in streams:
        _, rva = streams[STREAM_EXCEPTION]
        code, _, _, address, nparams = struct.unpack_from("<IIQQI", data, rva + 8)
        dump.code, dump.address = code, address
        dump.info = list(struct.unpack_from("<15Q", data, rva + 8 + 32))[: min(nparams, 15)]
        ctx_size, ctx_rva = struct.unpack_from("<II", data, rva + 160)
        if ctx_size >= 0x100:
            dump.rsp, dump.rip = struct.unpack_from("<Q", data, ctx_rva + 0x98)[0], struct.unpack_from("<Q", data, ctx_rva + 0xF8)[0]

    if dump.rsp is not None:
        wanted = [m for m in dump.modules if any(s in m.short.lower() for s in interesting)]
        for start, size, offset in ranges:
            if start <= dump.rsp < start + size:
                begin = offset + (dump.rsp - start)
                end = min(offset + size, begin + 256 * 1024)
                for pos in range(begin, end - 7, 8):
                    (value,) = struct.unpack_from("<Q", data, pos)
                    for m in wanted:
                        if m.base <= value < m.base + m.size:
                            dump.stack.append((m.short, value - m.base))
                            break
                    if len(dump.stack) >= max_frames:
                        break
                break
        else:
            dump.note = "the crashing thread's stack is not in the dump"
    return dump


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
