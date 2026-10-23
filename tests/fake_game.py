"""A fake game for the tests: answers the LuaRepl protocol from a thread, the way UE4SS (native) or VibeBridge
(heartbeat + single slot) does, without running any Lua."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

ID = re.compile(r"^([0-9a-f]{8,32})\.request\.json$")


class FakeGame(threading.Thread):
    """mode "native": per-id request files, any started mod. mode "bridge": heartbeat + slot, own state only.

    Code it understands: "1 + 1" -> 2; "error(...)" -> an error; "hang" -> never answers;
    "print(...)" -> the argument is printed; anything else is echoed back as a quoted string value."""

    def __init__(self, folder: Path, mode: str = "native", mods=("Alpha", "Beta")):
        super().__init__(daemon=True)
        self.folder, self.mode = folder, mode
        self.mods = ["VibeBridge"] if mode == "bridge" else list(mods)
        self.stop_event = threading.Event()
        self.seen: list[dict] = []
        self.folder.mkdir(parents=True, exist_ok=True)

    def stop(self):
        self.stop_event.set()
        self.join(5)

    def _write(self, name: str, body: dict):
        tmp = self.folder / (name + ".tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        os.replace(tmp, self.folder / name)

    def _answer(self, request_id: str, request: dict):
        self.seen.append(request)
        a = {"protocol": 1, "id": request_id, "ok": False, "error": "", "values": [], "prints": [], "mods": [],
             "truncated": False, "elapsed_ms": 0.01}
        if request.get("protocol") != 1:
            a["error"] = f"Unsupported protocol {request.get('protocol')}"
        elif request.get("op") == "list":
            a["ok"], a["mods"] = True, self.mods
        elif request.get("op") != "exec":
            a["error"] = f"Unknown op '{request.get('op')}'"
        elif request.get("mod") not in self.mods:
            a["error"] = f"No started Lua mod is named '{request.get('mod')}'"
        else:
            code = request.get("code", "")
            if code == "hang":
                return
            if code == "1 + 1":
                a["ok"], a["values"] = True, ["2"]
            elif code.startswith("error("):
                a["error"] = "ExternalLuaRepl:1: boom\nstack traceback:\n\t[C]: in function 'error'"
            elif code.startswith("print("):
                a["ok"], a["prints"] = True, [code[len("print("):-1]]
            else:
                a["ok"], a["values"] = True, ['"' + code + '"']
        self._write(f"{request_id}.response.json", a)

    def run(self):
        while not self.stop_event.is_set():
            if self.mode == "bridge":
                self._write("bridge.json", {"protocol": 1, "bridge": "VibeBridge", "version": "fake", "mod": "VibeBridge",
                                            "time": int(time.time())})
                slot = self.folder / "bridge.request.json"
                if slot.exists():
                    try:
                        request = json.loads(slot.read_text(encoding="utf-8"))
                        slot.unlink()
                    except (OSError, ValueError):
                        request = None
                    if request is not None:
                        self._answer(str(request.get("id", "")), request)
            else:
                for entry in sorted(self.folder.iterdir()):
                    m = ID.match(entry.name)
                    if not m or not entry.is_file():
                        continue
                    try:
                        request = json.loads(entry.read_text(encoding="utf-8"))
                        entry.unlink()
                    except (OSError, ValueError):
                        continue
                    self._answer(m.group(1), request)
            time.sleep(0.005)
