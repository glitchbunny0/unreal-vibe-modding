"""`uvmod check`: read a mod's Lua against the reflection index and report the calls that crash UE4SS natively before
the game ever runs them: a string or nil literal where a UFunction takes an FName. Also flags argument counts no
function of that name accepts (a Lua error in UE4SS, cheaper to see here)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .index import Index

# Methods of UE4SS's own Lua types: never UFunctions, whatever the game has with the same name.
UE4SS_METHODS = {
    "GetFullName", "GetFName", "GetAddress", "GetClass", "GetOuter", "IsAnyClass", "Reflection", "GetPropertyValue",
    "SetPropertyValue", "IsClass", "GetWorld", "IsA", "HasAllFlags", "HasAnyFlags", "HasAnyInternalFlags",
    "CallFunction", "ProcessConsoleExec", "type", "IsValid", "get", "set", "Get", "Set", "ToString", "GetArrayNum",
    "GetArrayMax", "GetArrayAddress", "GetArrayDataAddress", "ForEach", "ForEachProperty", "ForEachFunction",
    "GetSuperStruct", "GetCDO", "IsChildOf", "GetLevel", "SpawnActor", "Empty", "GetComparisonIndex",
    "GetFunctionFlags", "SetFunctionFlags", "GetStruct", "GetPropertyClass", "GetInner", "GetEnum", "GetNameByValue",
    "ForEachName", "GetEnumNameByIndex", "ContainerPtrToValuePtr", "ImportText", "GetOffset_Internal", "Contains",
    "Add", "Remove", "Find", "Clear", "IsInGameThread", "AddToRoot", "RemoveFromRoot",
}
CALL = re.compile(r":\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")
STRING_LITERAL = re.compile(r"""^(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\[(=*)\[.*?\]\1\])$""", re.S)


@dataclass
class Finding:
    file: str
    line: int
    col: int
    severity: str  # error | warning
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}:{self.col}: {self.severity}: {self.message}"


def blank_comments(src: str, strings: bool = False) -> str:
    """The source with comments replaced by spaces (offsets kept); with `strings`, string contents too."""
    out = list(src)
    i, n = 0, len(src)

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            if strings:
                blank(i + 1, j)
            i = j + 1
        elif src.startswith("[", i) and re.match(r"\[=*\[", src[i:]):
            eq = re.match(r"\[(=*)\[", src[i:]).group(1)  # type: ignore[union-attr]
            end = src.find("]" + eq + "]", i)
            stop = n if end < 0 else end + len(eq) + 2
            if strings:
                blank(i + len(eq) + 2, stop - len(eq) - 2)
            i = stop
        elif src.startswith("--", i):
            m = re.match(r"--\[(=*)\[", src[i:])
            if m:
                end = src.find("]" + m.group(1) + "]", i)
                stop = n if end < 0 else end + len(m.group(1)) + 2
            else:
                stop = src.find("\n", i)
                stop = n if stop < 0 else stop
            for k in range(i, stop):
                if out[k] != "\n":
                    out[k] = " "
            i = stop
        else:
            i += 1
    return "".join(out)


def split_args(src: str, start: int) -> tuple[list[str], int] | None:
    """Arguments of a call whose '(' is at start-1: top-level comma-separated texts, and the index after ')'."""
    depth, i, n, arg_start, args = 0, start, len(src), start, []
    while i < n:
        c = src[i]
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            i = j + 1
            continue
        m = re.match(r"\[(=*)\[", src[i:i + 12]) if c == "[" else None
        if m:
            end = src.find("]" + m.group(1) + "]", i)
            i = n if end < 0 else end + len(m.group(1)) + 2
            continue
        if c in "({[":
            depth += 1
        elif c in ")}]":
            if depth == 0:
                if c != ")":
                    return None
                last = src[arg_start:i].strip()
                if last or args:
                    args.append(last)
                return args, i + 1
            depth -= 1
        elif c == "," and depth == 0:
            args.append(src[arg_start:i].strip())
            arg_start = i + 1
        i += 1
    return None


def check_source(index: Index, src: str, filename: str = "<lua>") -> list[Finding]:
    code = blank_comments(src)
    masked = blank_comments(src, strings=True)  # where calls are looked for: not inside strings
    findings: list[Finding] = []
    cache: dict[str, list[tuple[str, list]]] = {}
    for m in CALL.finditer(masked):
        name = m.group(1)
        if name in UE4SS_METHODS or name.islower():  # UFunctions are PascalCase; f:close(), s:find() are Lua's
            continue
        if name not in cache:
            cache[name] = [(path, [p for p in index.params(path) if p.role != "ret"]) for path, _ in index.functions_named(name)]
        candidates = cache[name]
        if not candidates:
            continue
        parsed = split_args(code, m.end())
        if parsed is None:
            continue
        args = parsed[0]
        line = code.count("\n", 0, m.start(1)) + 1
        col = m.start(1) - (code.rfind("\n", 0, m.start(1)) + 1) + 1
        for pos, arg in enumerate(args):
            is_text = bool(STRING_LITERAL.match(arg))
            if not (is_text or arg == "nil"):
                continue
            fname_in = [c for c in candidates if pos < len(c[1]) and c[1][pos].kind == "NameProperty"]
            if not fname_in:
                continue
            p = fname_in[0][1][pos]
            what = "a string" if is_text else "nil"
            fix = f'FName({arg})' if is_text else 'FName("None")'
            msg = (f"{name} argument {pos + 1} ({p.name}) is an FName: {what} here is a native crash in UE4SS, not a Lua "
                   f"error; pass {fix}")
            if len(fname_in) == len(candidates):
                findings.append(Finding(filename, line, col, "error", msg))
            else:
                findings.append(Finding(filename, line, col, "warning",
                                        msg + f" (if this is {short_owner(fname_in[0][0])}'s {name}; "
                                              f"{len(candidates)} functions have this name)"))
        counts = sorted({len(c[1]) for c in candidates})
        if len(args) not in counts:
            want = " or ".join(str(c) for c in counts[:4])
            findings.append(Finding(filename, line, col, "warning",
                                    f"{name} gets {len(args)} argument{'s' if len(args) != 1 else ''}; the functions of this name "
                                    f"take {want} (out parameters count: pass {{}})"))
    return findings


def short_owner(path: str) -> str:
    return re.split(r"[.]", path.rpartition(":")[0])[-1]


def check_paths(index: Index, paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for p in paths:
        root = Path(p)
        files = sorted(root.rglob("*.lua")) if root.is_dir() else [root]
        for f in files:
            try:
                src = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            findings += check_source(index, src, str(f))
    return findings
