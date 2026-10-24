"""End to end: the real VibeBridge mod (in tests/fake_ue4ss.lua, plain Lua 5.4) answering the real client."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_client import ROOT, GameDir  # noqa: E402
from uvmod import cli  # noqa: E402
from uvmod.game import Game  # noqa: E402

LUA = os.environ.get("LUA") or shutil.which("lua5.4") or shutil.which("lua") or "lua"


HAVE_LUA = bool(os.path.exists(LUA) or shutil.which(LUA))


class BridgeHarness:
    """A game folder with VibeBridge installed by `uvmod install-bridge`, run by tests/fake_ue4ss.lua."""

    def start(self, ini: bytes = b"[Debug]\r\nEnableHotReloadSystem = 0\r\n", folder: bool = True):
        self.dir = GameDir()
        (self.dir.ue4ss / "UE4SS-settings.ini").write_bytes(ini)
        env = {"UVMOD_UE4SS": str(self.dir.ue4ss), "UVMOD_TRANSPORT": "local", "UVMOD_TIMEOUT": "5", "UVMOD_CONFIG": "/nonexistent",
               "UVMOD_JOURNAL": self.dir.journal}
        os.environ.update(env)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["install-bridge"]), 0)
        if not folder:
            shutil.rmtree(self.dir.folder)
        self.proc = subprocess.Popen([LUA, str(ROOT / "tests" / "fake_ue4ss.lua"), str(self.dir.ue4ss), "60"])
        self.game = Game(self.dir.settings(UVMOD_TIMEOUT="5"))
        for _ in range(200):
            if "VibeBridge] v" in self.log():
                break
            time.sleep(0.02)

    def log(self) -> str:
        return (self.dir.ue4ss / "UE4SS.log").read_text(encoding="utf-8", errors="replace")

    def tearDown(self):
        (self.dir.ue4ss / "STOP").touch()
        self.proc.wait(10)
        self.dir.close()
        for k in ("UVMOD_UE4SS", "UVMOD_TRANSPORT", "UVMOD_TIMEOUT", "UVMOD_CONFIG", "UVMOD_JOURNAL"):
            os.environ.pop(k, None)


@unittest.skipUnless(HAVE_LUA, "needs Lua 5.4 (set LUA=/path/to/lua)")
class BridgeEndToEnd(BridgeHarness, unittest.TestCase):
    def run_lua(self, code, limit=None):
        r = self.game.exec(code, None, limit)
        self.assertIsNotNone(r.answer, f"no answer to {code!r}")
        self.assertEqual(r.delivery, "bridge")
        return r.answer

    def test_requests(self):
        self.start()
        self.assertIn("running: answers requests", self.log())
        self.assertEqual(self.game.mods().answer["mods"], ["VibeBridge"])
        self.assertEqual(self.run_lua("1 + 1")["values"], ["2"])
        self.assertEqual(self.run_lua("return 1, 'two', nil, true, 2.5, 3.0")["values"], ["1", '"two"', "nil", "true", "2.5", "3.0"])
        a = self.run_lua("print('a', 1, nil) println('b')")
        self.assertEqual((a["ok"], a["prints"], a["values"]), (True, ["a\t1\tnil", "b"], []))
        a = self.run_lua("error('boom')")
        self.assertFalse(a["ok"])
        self.assertIn("boom", a["error"])
        self.assertIn("stack traceback", a["error"])
        self.run_lua("Counter = 41")
        self.assertEqual(self.run_lua("Counter + 1")["values"], ["42"])
        self.assertTrue(self.run_lua("local t = {} for i = 1, 11 do t[i] = i end return t")["values"][0].endswith("(11 entries)"))
        self.assertTrue(self.run_lua("local t = {} for i = 1, 150 do t[i] = i end return t")["values"][0].endswith("(100+ entries)"))
        self.assertTrue(self.run_lua("return setmetatable({}, {__pairs = function() error('no') end})")["values"][0].endswith("(0 entries)"))
        self.assertTrue(self.run_lua("string.rep")["values"][0].startswith("function: builtin: "))
        payload = 'quote " backslash \\ tab\t newline\n unicode \u00e9\u2603 \U0001f600 control \x01'
        self.assertEqual(self.run_lua("return [==[" + payload + "]==]")["values"], ['"' + payload + '"'])
        a = self.run_lua("print('h\\195\\169 \\255') return 'x\\0y'")
        self.assertEqual((a["prints"], a["values"]), (["h\u00e9 \ufffd"], ['"x\u0000y"']))
        a = self.run_lua("string.rep('y', 10000)")
        self.assertTrue(a["truncated"])
        self.assertEqual(len(a["values"][0]), 4096 + 3)
        self.assertIn("own Lua state", self.game.exec("1", "SomeOtherMod").answer["error"])
        self.assertIn("between 1 and 5000", self.run_bad_limit())

    def run_bad_limit(self):
        folder = self.dir.folder
        (folder / "bridge.request.json").write_text(json.dumps({"protocol": 1, "op": "exec", "mod": "VibeBridge", "code": "1",
                                                                "timeout_ms": 0, "id": "00000000aaaaaaaa"}))
        for _ in range(200):
            p = folder / "00000000aaaaaaaa.response.json"
            if p.exists():
                text = json.loads(p.read_text(encoding="utf-8"))["error"]
                p.unlink()
                return text
            time.sleep(0.02)
        return "no answer"

    def test_limits(self):
        self.start()
        for code in ("while true do end",
                     "while true do pcall(function() while true do end end) end",
                     "coroutine.wrap(function() while true do end end)()",
                     "while true do coroutine.resume(coroutine.create(function() while true do end end)) end"):
            a = self.run_lua(code)
            self.assertFalse(a["ok"], code)
            self.assertIn("limit of 250 ms", a["error"], code)
        self.assertIn("limit of 50 ms", self.run_lua("while true do end", 50)["error"])
        self.assertEqual(self.run_lua("1 + 1")["values"], ["2"])  # still answering afterwards

    def test_concurrent_callers_share_the_slot(self):
        self.start()
        results, errors = {}, []

        def caller(k):
            for i in range(15):
                r = self.game.exec(f"return '{k}-{i}'")
                if not r.answer or r.answer["values"] != [f'"{k}-{i}"']:
                    errors.append((k, i, r.answer))
            results[k] = True

        threads = [threading.Thread(target=caller, args=(k,)) for k in "abc"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual((sorted(results), errors), (["a", "b", "c"], []))

    def test_idle_when_native_repl_is_on(self):
        self.start(ini=b"[Debug]\r\nExternalLuaReplEnabled = 1\r\n")
        time.sleep(0.3)
        self.assertIn("idle: this UE4SS has its own external Lua REPL", self.log())
        self.assertFalse((self.dir.folder / "bridge.json").exists())

    def test_folder_missing(self):
        self.start(folder=False)
        self.assertIn("NOT running: the folder", self.log())


if __name__ == "__main__":
    unittest.main()
