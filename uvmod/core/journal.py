"""The journal: every request uvmod sends, written down before it goes out and marked when it comes back. When the
game dies, the last request without an answer is the first suspect (`uvmod crash` shows it)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .config import Settings

MAX_BYTES = 4 * 1024 * 1024
CODE_HEAD, CODE_TAIL = 400, 1600


def path(settings: Settings) -> Path | None:
    if settings.journal.lower() in ("off", "none", "0", "false"):
        return None
    if settings.journal:
        return Path(settings.journal).expanduser()
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / "uvmod" / "journal.jsonl"


def short_code(code: str) -> str:
    """Long code (a helper sent along with its call) keeps its start and its end, where the call is."""
    if len(code) <= CODE_HEAD + CODE_TAIL + 20:
        return code
    return f"{code[:CODE_HEAD]}\n...({len(code) - CODE_HEAD - CODE_TAIL} characters)...\n{code[-CODE_TAIL:]}"


def record(settings: Settings, entry: dict[str, Any]) -> None:
    p = path(settings)
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > MAX_BYTES:
            os.replace(p, p.with_suffix(".old.jsonl"))
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(entry, t=round(time.time(), 3)), ensure_ascii=False) + "\n")
    except OSError:
        pass  # the journal is a debugging aid; never let it stop a request


def tail(settings: Settings, count: int = 10) -> list[dict[str, Any]]:
    """The last `count` requests, each with its outcome merged in (`done`: ok | error | timeout, or missing)."""
    p = path(settings)
    if p is None or not p.exists():
        return []
    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 512 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    requests: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        rid = e.get("id")
        if not rid:
            continue
        if "done" in e:
            if rid in requests:
                requests[rid].update(done=e["done"], ms=e.get("ms"), error=e.get("error", ""))
        else:
            requests[rid] = e
            order.append(rid)
    return [requests[r] for r in order[-count:]]


def render(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "(the journal is empty)"
    out = []
    for e in entries:
        when = time.strftime("%H:%M:%S", time.localtime(e.get("t", 0)))
        done = e.get("done", "no answer")
        what = e.get("op", "?") + (f" in {e['mod']}" if e.get("mod") else "")
        out.append(f"{when} {what}: {done}" + (f" ({e['ms']} ms)" if e.get("ms") is not None else "")
                   + (f" {e['error']}" if e.get("error") else ""))
        code = e.get("code")
        if code:
            out += ["    " + line for line in code.splitlines()[-12:]]
    return "\n".join(out)
