"""Client tests against the fake game: protocol, settings, local transport, the core, the CLI and the MCP server."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_game import FakeGame  # noqa: E402
from uvmod import cli, protocol  # noqa: E402
from uvmod.config import ConfigError, Settings  # noqa: E402
from uvmod.game import Game, GameError  # noqa: E402

TRICKY = 'return "quote \\" backslash \\\\ tab\t newline\n unicode \u00e9\u2603 \U0001f600 nul-ish \x01"'


class GameDir:
    """A fake game install: <tmp>/Game/Binaries/Win64/{FakeGame-Win64-Shipping.exe, ue4ss/...}."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        win64 = Path(self.tmp.name) / "Game" / "Binaries" / "Win64"
        self.ue4ss = win64 / "ue4ss"
        (self.ue4ss / "Mods").mkdir(parents=True)
        (win64 / "FakeGame-Win64-Shipping.exe").write_bytes(b"")
        (self.ue4ss / "UE4SS-settings.ini").write_bytes(b"[Debug]\r\nExternalLuaReplEnabled = 1\r\nEnableHotReloadSystem = 1\r\n")
        (self.ue4ss / "UE4SS.log").write_bytes(b"[x] UE4SS - v3.0.1\r\n[x] [ExternalLuaRepl] Enabled.\r\n"
                                               b"[x] [ExternalLuaRepl] Request 0123abcd for mod 'Alpha'\r\nother line\r\n")
        self.folder = self.ue4ss / "LuaRepl"
        self.journal = str(Path(self.tmp.name) / "journal.jsonl")

    def settings(self, **kw) -> Settings:
        env = {"UVMOD_UE4SS": str(self.ue4ss), "UVMOD_TRANSPORT": "local", "UVMOD_TIMEOUT": "2", "UVMOD_JOURNAL": self.journal}
        env.update(kw)
        return Settings.load(env)

    def close(self):
        self.tmp.cleanup()


class ProtocolTests(unittest.TestCase):
    def test_ids(self):
        ids = {protocol.new_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(protocol.ID_PATTERN.match(i) for i in ids))

    def test_encode_and_limits(self):
        body = json.loads(protocol.encode(protocol.exec_request("M", "x", 50), "abcd1234"))
        self.assertEqual(body, {"protocol": 1, "op": "exec", "mod": "M", "code": "x", "timeout_ms": 50, "id": "abcd1234"})
        self.assertNotIn("id", json.loads(protocol.encode(protocol.list_request())))
        for bad in (0, 5001):
            with self.assertRaises(ValueError):
                protocol.exec_request("M", "x", bad)
        with self.assertRaises(ValueError):
            protocol.exec_request("", "x")

    def test_render(self):
        ok = protocol.parse_answer('{"ok": true, "values": ["2"], "prints": ["hi"]}')
        self.assertEqual(protocol.render_text(ok), "hi\n=> 2\nOK")
        bad = protocol.parse_answer('{"ok": false, "error": "boom", "truncated": true}')
        self.assertTrue(protocol.render_text(bad).startswith("ERROR: boom\n(truncated"))
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_answer("not json")


class SettingsTests(unittest.TestCase):
    def test_file_env_and_inference(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d) / "config"
            conf.write_text('UVMOD_UE4SS="C:\\Games\\X\\ue4ss"\nUVMOD_HOST=player@box\nUVMOD_TIMEOUT=7\n# comment\nOTHER=1\n')
            old = dict(os.environ)
            try:
                for k in [k for k in os.environ if k.startswith("UVMOD_")]:
                    del os.environ[k]
                os.environ["UVMOD_CONFIG"] = str(conf)
                s = Settings.load()
                self.assertEqual((s.ue4ss, s.transport, s.host, s.timeout), ("C:\\Games\\X\\ue4ss", "ssh", "player@box", 7.0))
                os.environ["UVMOD_TRANSPORT"] = "local"
                self.assertEqual(Settings.load().transport, "local")
                os.environ["UVMOD_TRANSPORT"] = "ftp"
                with self.assertRaises(ConfigError):
                    Settings.load()
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_missing_ue4ss(self):
        with self.assertRaises(ConfigError):
            Settings.load({"UVMOD_UE4SS": "", "UVMOD_CONFIG": "/nonexistent"})


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.dir = GameDir()
        self.fake = FakeGame(self.dir.folder, "native")
        self.fake.start()
        self.game = Game(self.dir.settings())

    def tearDown(self):
        self.fake.stop()
        self.dir.close()

    def test_exec_roundtrip(self):
        r = self.game.exec("1 + 1", "Alpha")
        self.assertEqual((r.delivery, r.ok, r.answer["values"]), ("native", True, ["2"]))

    def test_code_survives_encoding(self):
        r = self.game.exec(TRICKY, "Alpha")
        self.assertEqual(r.answer["values"], ['"' + TRICKY + '"'])

    def test_errors_prints_and_unknown_mod(self):
        self.assertFalse(self.game.exec("error('x')", "Alpha").ok)
        self.assertEqual(self.game.exec("print(hello)", "Alpha").answer["prints"], ["hello"])
        r = self.game.exec("1 + 1", "Nope")
        self.assertIn("No started Lua mod is named 'Nope'", r.answer["error"])

    def test_resolve_mod(self):
        with self.assertRaises(GameError) as e:
            self.game.exec("1 + 1")
        self.assertIn("Alpha, Beta", str(e.exception))
        self.assertTrue(Game(self.dir.settings(UVMOD_MOD="Beta")).exec("1 + 1").ok)

    def test_limit_is_sent(self):
        self.game.exec("1 + 1", "Alpha", 1234)
        self.assertEqual(self.fake.seen[-1]["timeout_ms"], 1234)

    def test_timeout_withdraws_the_request(self):
        game = Game(self.dir.settings(UVMOD_TIMEOUT="0.3"))
        self.fake.stop()
        r = game.exec("1 + 1", "Alpha")
        self.assertIsNone(r.answer)
        self.assertEqual(r.delivery, "native")
        self.assertEqual([p.name for p in self.dir.folder.iterdir()], [])

    def test_hang_times_out(self):
        r = Game(self.dir.settings(UVMOD_TIMEOUT="0.3")).exec("hang", "Alpha")
        self.assertIsNone(r.answer)

    def test_no_files_left_behind(self):
        for _ in range(5):
            self.game.exec("1 + 1", "Alpha")
        time.sleep(0.05)
        self.assertEqual([p.name for p in self.dir.folder.iterdir()], [])

    def test_status_log_push_swap(self):
        s = self.game.status()
        self.assertEqual(s.process, "FakeGame-Win64-Shipping")
        self.assertEqual(s.repl_setting, "ExternalLuaReplEnabled = 1")
        self.assertEqual(s.hot_reload_setting, "EnableHotReloadSystem = 1")
        self.assertTrue(s.repl_folder)
        self.assertEqual(len(s.log_lines), 2)
        self.assertEqual(self.game.log(1), ["other line"])
        self.assertEqual(self.game.log(5, "externallua")[0], "[x] [ExternalLuaRepl] Enabled.")
        self.assertNotIn("Request", "".join(s.log_lines))
        src = Path(self.dir.tmp.name) / "src" / "MyMod"
        (src / "Scripts").mkdir(parents=True)
        (src / "Scripts" / "main.lua").write_text("return {}")
        (src / "hotswap").mkdir()
        (src / "hotswap" / "junk").write_text("x")
        self.assertIn("pushed MyMod", self.game.push(str(src)))
        self.assertTrue((self.dir.ue4ss / "Mods" / "MyMod" / "Scripts" / "main.lua").exists())
        self.assertFalse((self.dir.ue4ss / "Mods" / "MyMod" / "hotswap" / "junk").exists())
        ok, text = Game(self.dir.settings(UVMOD_TIMEOUT="0.3")).swap(str(src))
        self.assertFalse(ok)
        self.assertIn("not a hot-swap shell", text)
        self.assertFalse((self.dir.ue4ss / "Mods" / "MyMod" / "hotswap" / "req").exists())

    def test_cli(self):
        os.environ.update({"UVMOD_UE4SS": str(self.dir.ue4ss), "UVMOD_TRANSPORT": "local", "UVMOD_TIMEOUT": "2", "UVMOD_CONFIG": "/nonexistent",
                           "UVMOD_JOURNAL": self.dir.journal})
        try:
            def run(*argv):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    rc = cli.main(list(argv))
                return rc, out.getvalue() + err.getvalue()

            self.assertEqual(run("lua", "1 + 1", "--mod", "Alpha"), (0, "=> 2\nOK\n"))
            rc, out = run("lua", "error('x')", "--mod", "Alpha")
            self.assertEqual(rc, 1)
            self.assertIn("ERROR:", out)
            rc, out = run("lua", "1 + 1")
            self.assertEqual(rc, 3)
            self.assertIn("several mods", out)
            rc, out = run("mods", "--json")
            self.assertEqual((rc, json.loads(out)["mods"], json.loads(out)["delivery"]), (0, ["Alpha", "Beta"], "native"))
            rc, out = run("status")
            self.assertEqual(rc, 0)
            self.assertIn("repl: UE4SS external REPL answering", out)
            self.assertIn("target: none set: pass --mod", out)
            os.environ["UVMOD_MOD"] = "Beta"
            self.assertIn("target: Beta\n", run("status")[1])
            os.environ["UVMOD_MOD"] = "Gamma"
            self.assertIn("target: Gamma  <- not among", run("status")[1])
            del os.environ["UVMOD_MOD"]
            self.assertIn("<- unsafe", out)
            self.assertEqual(run("bogus")[0], 2)
            self.assertEqual(run("help")[0], 0)
        finally:
            for k in ("UVMOD_UE4SS", "UVMOD_TRANSPORT", "UVMOD_TIMEOUT", "UVMOD_CONFIG", "UVMOD_JOURNAL"):
                os.environ.pop(k, None)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.dir = GameDir()
        self.fake = FakeGame(self.dir.folder, "bridge")
        self.fake.start()
        time.sleep(0.05)
        self.game = Game(self.dir.settings())

    def tearDown(self):
        self.fake.stop()
        self.dir.close()

    def test_delivery_and_default_mod(self):
        r = self.game.exec("1 + 1")
        self.assertEqual((r.delivery, r.answer["values"]), ("bridge", ["2"]))
        self.assertEqual(self.fake.seen[-1]["mod"], "VibeBridge")
        self.assertEqual(len(self.fake.seen[-1]["id"]), 16)

    def test_sequential_requests_share_the_slot(self):
        for i in range(10):
            self.assertEqual(self.game.exec(f"value {i}").answer["values"], [f'"value {i}"'])

    def test_stale_heartbeat_means_native(self):
        self.fake.stop()
        old = time.time() - 60
        os.utime(self.dir.folder / "bridge.json", (old, old))
        r = Game(self.dir.settings(UVMOD_TIMEOUT="0.2")).exec("1 + 1", "VibeBridge")
        self.assertEqual(r.delivery, "native")

    def test_timeout_withdraws_only_its_own_slot_request(self):
        self.fake.stop()
        (self.dir.folder / "bridge.json").touch()
        r = Game(self.dir.settings(UVMOD_TIMEOUT="0.3")).exec("1 + 1", "VibeBridge")
        self.assertIsNone(r.answer)
        self.assertFalse((self.dir.folder / "bridge.request.json").exists())


class MissingFolderTests(unittest.TestCase):
    def test_missing(self):
        d = GameDir()
        try:
            r = Game(d.settings()).exec("1 + 1", "Alpha")
            self.assertEqual((r.delivery, r.answer), ("missing", None))
        finally:
            d.close()


class McpTests(unittest.TestCase):
    def setUp(self):
        self.dir = GameDir()
        self.fake = FakeGame(self.dir.folder, "native", mods=("Alpha",))
        self.fake.start()
        env = dict(os.environ, UVMOD_UE4SS=str(self.dir.ue4ss), UVMOD_TRANSPORT="local", UVMOD_TIMEOUT="2", UVMOD_CONFIG="/nonexistent",
                   UVMOD_JOURNAL=self.dir.journal, PYTHONPATH=str(ROOT))
        self.proc = subprocess.Popen([sys.executable, "-m", "uvmod", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, env=env, cwd=str(ROOT))

    def tearDown(self):
        self.proc.stdin.close()
        self.proc.wait(10)
        self.proc.stdout.close()
        self.fake.stop()
        self.dir.close()

    def rpc(self, method, params=None, msg_id=1):
        msg = {"jsonrpc": "2.0", "method": method}
        if msg_id is not None:
            msg["id"] = msg_id
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline()) if msg_id is not None else None

    def test_session(self):
        init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], "unreal-vibe-modding")
        self.rpc("notifications/initialized", msg_id=None)
        tools = {t["name"] for t in self.rpc("tools/list", msg_id=2)["result"]["tools"]}
        self.assertEqual(tools, {"lua", "mods", "status", "log", "screenshot", "watch", "events", "wait", "unwatch", "push", "swap",
                                 "inspect", "call", "snapshot", "game", "api", "input", "calls"})
        r = self.rpc("tools/call", {"name": "lua", "arguments": {"code": "1 + 1"}}, msg_id=3)["result"]
        self.assertFalse(r["isError"])
        self.assertTrue(r["content"][0]["text"].startswith("=> 2\nOK\n(UE4SS,"))
        r = self.rpc("tools/call", {"name": "lua", "arguments": {"code": "error('x')"}}, msg_id=4)["result"]
        self.assertTrue(r["isError"])
        r = self.rpc("tools/call", {"name": "log", "arguments": {"lines": 1}}, msg_id=5)["result"]
        self.assertEqual(r["content"][0]["text"], "other line")
        self.assertEqual(self.rpc("tools/call", {"name": "nope"}, msg_id=6)["error"]["code"], -32602)
        self.assertEqual(self.rpc("bogus/method", msg_id=7)["error"]["code"], -32601)
        self.assertEqual(self.rpc("ping", msg_id=8)["result"], {})
        old = self.rpc("initialize", {"protocolVersion": "1999-01-01"}, msg_id=9)
        self.assertEqual(old["result"]["protocolVersion"], "2025-06-18")


if __name__ == "__main__":
    unittest.main()
