"""Per-game notes: what was learned about one game (names that lie, clamp limits, safe calls, how to reach a test
scene), kept beside its reflection index so the next session starts with it. Plain Markdown, one file per game."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .index import project_of_ue4ss


def path(ue4ss: str) -> Path:
    project = project_of_ue4ss(ue4ss) or "unknown-game"
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "uvmod" / "games" / project / "notes.md"


def read(ue4ss: str) -> str:
    p = path(ue4ss)
    if not p.exists():
        return f"(no notes for {project_of_ue4ss(ue4ss) or 'this game'} yet: add what you learn, it is read at the next session's start)"
    return p.read_text(encoding="utf-8")


def add(ue4ss: str, text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise ValueError("a note needs text")
    p = path(ue4ss)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", encoding="utf-8") as f:
        if new:
            f.write(f"# Notes: {project_of_ue4ss(ue4ss) or 'game'}\n\n")
        f.write(f"- {time.strftime('%Y-%m-%d')}: {text}\n")
    return f"noted in {p}"
