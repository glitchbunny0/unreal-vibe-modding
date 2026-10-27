"""Cyberpunk 2077 support: settings, the CET VibeBridge mod (in tests/fake_cet.lua, a stand-in for CET's mod sandbox)
answering the real client, CETBridge's handlers through `cet`, and the commands that refuse to run with CET.

The bridge runs under Lua 5.4 always, and under LuaJIT (CET's Lua) too when lupa is installed."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from uvmod import cet, cli, engines  # noqa: E402
from uvmod.config import ConfigError, Settings  # noqa: E402
from uvmod.game import Game, GameError  # noqa: E402
from uvmod.mcp import Server, tools_for  # noqa: E402

LUA54 = os.environ.get("LUA") or shutil.which("lua5.4") or shutil.which("lua")
RUNTIMES = {}
if LUA54:
    RUNTIMES["lua5.4"] = [LUA54]
if importlib.util.find_spec("lupa"):
    RUNTIMES["luajit"] = [sys.executable, str(ROOT / "tests" / "luajit_run.py")]

ENV_KEYS = ("UVMOD_CET", "UVMOD_UE4SS", "UVMOD_TRANSPORT", "UVMOD_TIMEOUT", "UVMOD_CONFIG", "UVMOD_JOURNAL", "UVMOD_MOD")


class CetDir:
    """A fake Cyberpunk 2077 install: <tmp>/Game/bin/x64/{Cyberpunk2077.exe, plugins/cyber_engine_tweaks/...}."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        x64 = Path(self.tmp.name) / "Game" / "bin" / "x64"
        self.cet = x64 / "plugins" / "cyber_engine_tweaks"
        (self.cet / "mods").mkdir(parents=True)
        (x64 / "Cyberpunk2077.exe").write_bytes(b"")
        (self.cet / "scripting.log").write_bytes(b"[x] [info] Mod Other loaded! ('x')\n")
        self.journal = str(Path(self.tmp.name) / "journal.jsonl")
        self.env = {"UVMOD_CET": str(self.cet), "UVMOD_TRANSPORT": "local", "UVMOD_TIMEOUT": "5",
                    "UVMOD_CONFIG": "/nonexistent", "UVMOD_JOURNAL": self.journal}

    def settings(self, **kw) -> Settings:
        return Settings.load(dict(self.env, **kw))

    def close(self):
        self.tmp.cleanup()


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class SettingsAndCatalog(unittest.TestCase):
    def test_engine_from_settings(self):
        d = CetDir()
        try:
            s = d.settings()
            self.assertTrue(s.is_cet)
            self.assertEqual(s.ue4ss, str(d.cet))
            with self.assertRaises(ConfigError):
                Settings.load(dict(d.env, UVMOD_UE4SS="/somewhere"))
            with self.assertRaises(ConfigError):
                Settings.load({"UVMOD_CONFIG": "/nonexistent"})
        finally:
            d.close()

    def test_engine_paths(self):
        self.assertEqual(engines.CET.repl, "mods/VibeBridge/LuaRepl")
        self.assertEqual(engines.UE4SS.repl, "LuaRepl")
        self.assertEqual(engines.CET.process, "Cyberpunk2077")

    def test_catalog_matches_the_lua_handlers(self):
        self.assertEqual(sorted(cet.HANDLERS), cet.lua_handlers())
        self.assertFalse(set(cet.READS) & set(cet.CHANGES))

    def test_parse_args(self):
        self.assertEqual(cet.parse_args('{"x": 1}'), {"x": 1})
        self.assertEqual(cet.parse_args("x=1 y=-2.5 name=Items.Foo on=true"), {"x": 1, "y": -2.5, "name": "Items.Foo", "on": True})
        self.assertEqual(cet.parse_args(""), {})
        with self.assertRaises(ValueError):
            cet.parse_args("nonsense")

    def test_mcp_tools(self):
        names = [t["name"] for t in tools_for("cet")]
        self.assertEqual(names, ["lua", "mods", "status", "log", "screenshot", "push", "game", "cet"])
        self.assertIn("watch", [t["name"] for t in tools_for("ue4ss")])
        game = next(t for t in tools_for("cet") if t["name"] == "game")
        self.assertNotIn("console", game["inputSchema"]["properties"]["action"]["enum"])

    def test_capture_copies_only_the_game(self):
        from uvmod import capture

        script = capture.script("Cyber'punk", r"C:\T\shot.png", r"C:\T\shot.txt")
        # the foreground check comes before anything is copied from the screen
        self.assertLess(script.index("GetForegroundWindow()"), script.index("CopyFromScreen"))
        self.assertIn("Get-Process -Name 'Cyber''punk'", script)

    def test_ue_only_commands_refuse(self):
        d = CetDir()
        saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ.update(d.env)
        try:
            for argv in (["world"], ["watch", "/Script/X:Y"], ["index"], ["input", "list"], ["console", "x"]):
                code, out, err = run_cli(argv)
                self.assertEqual(code, cli.EXIT_USAGE, argv)
                self.assertIn("needs UE4SS", err)
            game = Game(d.settings())
            with self.assertRaises(GameError):
                game.burst(3)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            d.close()


class CetBridgeHarness:
    runtime = "lua5.4"

    def setUp(self):
        if self.runtime not in RUNTIMES:
            self.skipTest(f"needs {self.runtime}")
        self.dir = CetDir()
        self.saved = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ.update(self.dir.env)
        code, out, err = run_cli(["install-bridge"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("mods/VibeBridge/LuaRepl", out)
        self.proc = subprocess.Popen(RUNTIMES[self.runtime] + [str(ROOT / "tests" / "fake_cet.lua"), str(self.dir.cet), "60"])
        self.game = Game(self.dir.settings())
        for _ in range(300):
            if "[VibeBridge] v" in self.log():
                break
            time.sleep(0.02)

    def log(self) -> str:
        return (self.dir.cet / "scripting.log").read_text(encoding="utf-8", errors="replace")

    def tearDown(self):
        (self.dir.cet / "STOP").touch()
        self.proc.wait(10)
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.dir.close()

    def exec(self, code, **kw):
        result = self.game.exec(code, **kw)
        self.assertIsNotNone(result.answer, f"no answer; log:\n{self.log()}")
        self.assertEqual(result.delivery, "bridge")
        return result.answer

    # --- the protocol ---------------------------------------------------------------------------------------------

    def test_installed_and_ready(self):
        self.assertIn("35 CETBridge handlers", self.log())
        info = json.loads((self.dir.cet / "mods" / "VibeBridge" / "LuaRepl" / "bridge.json").read_text())
        self.assertEqual((info["bridge"], info["mod"], info["cet"]), ("VibeBridge", "VibeBridge", "v1.38.1-fake"))
        listed = self.game.mods()
        self.assertEqual(listed.answer["mods"], ["VibeBridge"])

    def test_values_prints_and_globals(self):
        self.assertEqual(self.exec("1 + 1")["values"], ["2"])
        a = self.exec('print("hi", 3) return nil, "s", {1, 2}')
        self.assertEqual(a["prints"], ["hi\t3"])
        self.assertEqual(a["values"][:2], ["nil", '"s"'])
        self.assertTrue(a["values"][2].endswith("(2 entries)"))
        self.exec("counter = 41")
        self.assertEqual(self.exec("counter + 1")["values"], ["42"])
        self.assertNotIn("hi\t3", self.log())  # captured prints stay out of the log

    def test_errors(self):
        a = self.exec('error("boom")')
        self.assertFalse(a["ok"])
        self.assertIn("boom", a["error"])
        self.assertFalse(self.exec("this is not lua")["ok"])
        self.assertEqual(self.exec("type(debug), type(load), type(utf8)")["values"], ['"nil"', '"nil"', '"nil"'])
        # print works again after a failed request
        self.assertEqual(self.exec('print("again")')["prints"], ["again"])

    def test_refusals(self):
        a = self.game.request({"protocol": 1, "op": "exec", "mod": "Other", "code": "1"})
        self.assertIn("its own environment", a.answer["error"])
        a = self.game.request({"protocol": 1, "op": "exec", "mod": "VibeBridge", "code": "1", "timeout_ms": 9000})
        self.assertIn("timeout_ms", a.answer["error"])
        a = self.game.request({"protocol": 2, "op": "list"})
        self.assertIn("Unsupported protocol", a.answer["error"])

    def test_json_and_chunks(self):
        text = "".join(v[1:-1] for v in self.exec('vb.json({n = 1.5, s = "\\195\\169", t = {1, 2, {a = true}}})')["values"])
        self.assertEqual(json.loads(text), {"n": 1.5, "s": "\u00e9", "t": [1, 2, {"a": True}]})
        values = self.exec('vb.chunks(string.rep("\\195\\169", 5000))')["values"]
        self.assertGreater(len(values), 2)
        self.assertEqual("".join(v[1:-1] for v in values), "\u00e9" * 5000)

    def test_cet_handlers(self):
        answer = self.game.cet_query("player_info")
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["result"]["level"], 57)
        self.assertEqual(answer["result"]["position"], {"x": -1050.25, "y": 1340.5, "z": 30})
        # cetbridge_fixes: absolute health beside the percentage, not the percentage as "health"
        self.assertEqual((answer["result"]["health"], answer["result"]["healthPercent"]), (432, 89))
        self.assertIn("unknown handler", self.game.cet_query("nope")["error"])
        code, out, _ = run_cli(["cet", "player_info"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["streetCred"], 50)
        code, out, _ = run_cli(["cet"])
        self.assertIn("teleport(x, y, z)", out)

    def test_fixed_handlers(self):
        # out parameters come back as (success, list): the list is used, not the boolean
        inv = self.game.cet_query("get_inventory")["result"]
        self.assertEqual(inv["total"], 2)
        self.assertEqual(inv["items"][0], {"id": "Items.money", "name": "Eurodollars", "quantity": 340925, "type": "gamedataItem_Record"})
        weapons = self.game.cet_query("get_inventory", {"type": "Weapon"})["result"]["items"]
        self.assertEqual([i["name"] for i in weapons], ["Satori"])
        near = self.game.cet_query("get_nearby_entities")["result"]
        self.assertEqual((near["count"], near["entities"][0]["name"], near["entities"][0]["distance"]), (1, "Cortes V5000 Valor", 5.0))
        # TweakDB search needs a record type; short names work, and records without a name have none
        found = self.game.cet_query("search_tweakdb", {"pattern": "KATANA"})["result"]
        self.assertEqual([(r["path"], r.get("name")) for r in found["results"]],
                         [("Items.Preset_Katana_Saburo", "Satori"), ("Items.w_katana_blade", None)])
        found = self.game.cet_query("search_tweakdb", {"pattern": "quadra", "type": "Vehicle"})["result"]
        self.assertEqual(found["results"][0]["type"], "gamedataVehicle_Record")
        # DumpType's text, parsed and filtered
        d = self.game.cet_query("dump_type", {"typeName": "PlayerPuppet"})["result"]
        self.assertEqual(d["functions"], ["IsInCombat() => (Bool)", "GetLevel(Int32, Bool) => (Float)"])
        self.assertEqual(d["properties"], ["quickSlotsManager: handle:QuickSlotsManager"])
        self.assertEqual(self.game.cet_query("dump_type", {"typeName": "PlayerPuppet", "pattern": "combat"})["result"]["functions"],
                         ["IsInCombat() => (Bool)"])
        self.assertIn("Type not found", self.game.cet_query("dump_type", {"typeName": "Nope"})["error"])
        self.assertIn("not possible", self.game.cet_query("set_weather", {"weather": "Rain"})["error"])
        o = self.game.cet_query("overview")["result"]
        self.assertEqual((o["level"], o["eddies"], o["time"]), (57, 340925, "day 76, 15:46"))
        self.assertEqual(o["position"], {"x": -1050.3, "y": 1340.5, "z": 30})

    def test_game_objects(self):
        a = self.exec("Game.GetPlayer(), FakeEntityID(), vb.json({id = FakeEntityID(), p = Game.GetPlayer()})")
        if self.runtime == "luajit":  # userdata handles and 64-bit cdata, as in CET
            self.assertEqual(a["values"][:2], ["object: PlayerPuppet", "1234567890123"])
            self.assertEqual(json.loads(a["values"][2][1:-1]), {"id": 1234567890123, "p": {"class": "PlayerPuppet"}})
        else:
            self.assertEqual(a["values"][1], "1234567890123")

    def test_status_and_mcp(self):
        code, out, _ = run_cli(["status"])
        self.assertEqual(code, 0, out)
        self.assertIn("VibeBridge: running (v0.1.0, CET v1.38.1-fake, 35 CETBridge handlers)", out)
        self.assertIn("repl: VibeBridge answering", out)
        self.assertNotIn("external REPL", out)
        self.assertIn("[VibeBridge] v0.1.0 ready", out)
        server = Server()
        listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertIn("cet", [t["name"] for t in listed["result"]["tools"]])
        reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": "cet", "arguments": {"handler": "player_info"}}})
        self.assertFalse(reply["result"]["isError"], reply)
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"])["level"], 57)
        reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "watch", "arguments": {}}})
        self.assertIn("unknown tool", reply["error"]["message"])


class CetBridgeLua54(CetBridgeHarness, unittest.TestCase):
    runtime = "lua5.4"


class CetBridgeLuaJIT(CetBridgeHarness, unittest.TestCase):
    runtime = "luajit"


if __name__ == "__main__":
    unittest.main()
