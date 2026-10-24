"""`uvmod new`: a mod project to start from (hot-swap shell, logic file, tests against a strict UE4SS stand-in), and
`uvmod mock`: the real signatures of the UFunctions a mod calls, written for those tests from the reflection index."""

from __future__ import annotations

import os
import re
import stat
from importlib import resources
from pathlib import Path

from .index import Index
from .lint import CALL, UE4SS_METHODS, blank_comments

NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,40}$")


def _walk(node, rel: Path):
    for child in node.iterdir():
        if child.is_dir():
            yield from _walk(child, rel / child.name)
        else:
            yield rel / child.name, child


def new(name: str, parent: str = ".") -> Path:
    if not NAME_PATTERN.match(name):
        raise ValueError("a mod name is a letter, then letters, digits or _ (it becomes the mod folder's name)")
    target = Path(parent) / name
    if target.exists():
        raise ValueError(f"{target} exists already")
    template = resources.files("uvmod") / "templates" / "mod"
    for rel, src in _walk(template, Path()):
        dest_rel = Path(str(rel).replace("{{NAME}}", name))
        if dest_rel.name == "gitignore.txt":
            dest_rel = dest_rel.with_name(".gitignore")
        dest = target / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        try:
            dest.write_text(data.decode("utf-8").replace("{{NAME}}", name), encoding="utf-8")
        except UnicodeDecodeError:
            dest.write_bytes(data)
        if dest.suffix == ".sh":
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def called_functions(project: Path) -> list[str]:
    """Names of the methods the mod's Lua calls with ':' (outside strings and comments), tests excluded."""
    names: set[str] = set()
    for f in project.rglob("*.lua"):
        if "tests" in f.relative_to(project).parts:
            continue
        code = blank_comments(f.read_text(encoding="utf-8", errors="replace"), strings=True)
        names.update(m.group(1) for m in CALL.finditer(code) if m.group(1) not in UE4SS_METHODS and not m.group(1).islower())
    return sorted(names)


def lua_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def mock(project: str, ix: Index) -> tuple[Path, int, list[str]]:
    """Write <project>/tests/game_api.lua. Returns (path, functions written, names not in the index)."""
    root = Path(project)
    out = root / "tests" / "game_api.lua"
    if not out.parent.is_dir():
        raise ValueError(f"{root} has no tests/ folder (is it a project made by uvmod new?)")
    lines = ["-- Signatures of the UFunctions the mod calls, from the game's reflection index. Made by `uvmod mock`; run it",
             f"-- again after the mod calls new functions. {ix.header()}",
             "-- Each name maps to its candidates: { { name, kind, role }, ... } per function of that name.",
             "return {"]
    written, unknown = 0, []
    for name in called_functions(root):
        candidates = ix.functions_named(name)
        if not candidates:
            unknown.append(name)
            continue
        written += 1
        sigs = []
        for path, _ in candidates[:12]:
            params = ", ".join(f"{{ name = {lua_quote(p.name)}, kind = {lua_quote(p.kind)}, role = {lua_quote(p.role)} }}"
                               for p in ix.params(path))
            sigs.append(f"        {{ {params} }}, -- {path}")
        lines.append(f"    {name} = {{\n" + "\n".join(sigs) + "\n    },")
    lines.append("}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out, written, unknown
