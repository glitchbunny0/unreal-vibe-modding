"""The LuaRepl protocol (docs/protocol.md): request and answer messages, ids, and their text rendering."""

from __future__ import annotations

import json
import os
import re
from typing import Any

PROTOCOL = 1
ID_PATTERN = re.compile(r"^[0-9a-f]{8,32}$")
MAX_TIMEOUT_MS = 5000

# How fresh VibeBridge's heartbeat must be for the bridge to count as running.
BRIDGE_FRESH_SECONDS = 5.0
FOLDER = "LuaRepl"
BRIDGE_INFO = "bridge.json"
BRIDGE_SLOT = "bridge.request.json"


def new_id() -> str:
    return os.urandom(8).hex()


def exec_request(mod: str, code: str, timeout_ms: int | None = None) -> dict[str, Any]:
    if not mod:
        raise ValueError("exec needs a mod name")
    if timeout_ms is not None and not 1 <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be between 1 and {MAX_TIMEOUT_MS}")
    request: dict[str, Any] = {"protocol": PROTOCOL, "op": "exec", "mod": mod, "code": code}
    if timeout_ms is not None:
        request["timeout_ms"] = timeout_ms
    return request


def list_request() -> dict[str, Any]:
    return {"protocol": PROTOCOL, "op": "list"}


def encode(request: dict[str, Any], request_id: str | None = None) -> bytes:
    """The request as UTF-8 JSON. With an id, for the bridge slot, which carries the id inside."""
    body = dict(request)
    if request_id is not None:
        body["id"] = request_id
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class ProtocolError(Exception):
    pass


def parse_answer(text: str) -> dict[str, Any]:
    try:
        answer = json.loads(text)
    except ValueError as e:
        raise ProtocolError(f"the answer is not JSON: {e}") from None
    if not isinstance(answer, dict):
        raise ProtocolError("the answer is not a JSON object")
    answer.setdefault("ok", False)
    answer.setdefault("error", "")
    for key in ("values", "prints", "mods"):
        answer.setdefault(key, [])
    answer.setdefault("truncated", False)
    return answer


def render_text(answer: dict[str, Any]) -> str:
    """The answer as lines: prints, then one "=> value" per value (or the mods), then OK; or ERROR: ..."""
    lines = list(answer.get("prints", []))
    if answer.get("ok"):
        lines += [f"=> {v}" for v in answer.get("values", [])]
        lines += list(answer.get("mods", []))
        lines.append("OK")
    else:
        lines.append(f"ERROR: {answer.get('error', '')}")
    if answer.get("truncated"):
        lines.append("(truncated: the game keeps at most 64 values, 4 KiB each, and 64 KiB of prints)")
    return "\n".join(lines)
