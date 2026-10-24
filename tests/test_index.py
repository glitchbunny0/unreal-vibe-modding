"""The reflection index and `check`: built from a small .jmap made here (the format UE4SS's DumpJMAP writes), queried
through the Index class, the CLI and the MCP server; and `uvmod index` end to end through the fake bridge."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bridge_e2e import HAVE_LUA, BridgeHarness  # noqa: E402
from uvmod import cli, index  # noqa: E402
from uvmod.lint import check_source  # noqa: E402
from uvmod.mcp import Server  # noqa: E402

P = "CPF_Parm"
JMAP = {
    "metadata": {"source": "MyGame-Win64-Shipping.exe", "engine_version": {"major": 5, "minor": 6},
                 "build_change_list": "20702", "timestamp": "2026-10-24T14:32:40Z"},
    "objects": {
        "/Script/CoreUObject.Object": {"type": "Class", "super_struct": None, "properties": []},
        "/Script/CoreUObject.Vector": {"type": "ScriptStruct", "super_struct": None, "properties": [
            {"name": "X", "type": "DoubleProperty", "flags": ""}]},
        "/Script/Engine.Actor": {"type": "Class", "super_struct": "/Script/CoreUObject.Object", "properties": [
            {"name": "bHidden", "type": "BoolProperty", "flags": "CPF_Edit"},
            {"name": "Tags", "type": "ArrayProperty", "inner": {"name": "Tags", "type": "NameProperty"}, "flags": ""}]},
        "/Script/Engine.Actor:ActorHasTag": {"type": "Function", "outer": "/Script/Engine.Actor",
            "function_flags": "FUNC_Final | FUNC_Native | FUNC_BlueprintCallable | FUNC_BlueprintPure | FUNC_Const",
            "properties": [{"name": "Tag", "type": "NameProperty", "flags": P},
                           {"name": "ReturnValue", "type": "BoolProperty", "flags": f"{P} | CPF_OutParm | CPF_ReturnParm"}]},
        "/Script/Engine.Actor:K2_SetActorLocation": {"type": "Function", "outer": "/Script/Engine.Actor",
            "function_flags": "FUNC_Native | FUNC_BlueprintCallable", "properties": [
                {"name": "NewLocation", "type": "StructProperty", "struct": "/Script/CoreUObject.Vector", "flags": P},
                {"name": "bSweep", "type": "BoolProperty", "flags": P},
                {"name": "SweepHitResult", "type": "StructProperty", "struct": "/Script/Engine.HitResult", "flags": f"{P} | CPF_OutParm"},
                {"name": "bTeleport", "type": "BoolProperty", "flags": P},
                {"name": "ReturnValue", "type": "BoolProperty", "flags": f"{P} | CPF_OutParm | CPF_ReturnParm"}]},
        "/Script/Engine.Actor:GetActorBounds": {"type": "Function", "outer": "/Script/Engine.Actor",
            "function_flags": "FUNC_Native", "properties": [
                {"name": "bOnlyColliding", "type": "BoolProperty", "flags": P},
                {"name": "Origin", "type": "StructProperty", "struct": "/Script/CoreUObject.Vector", "flags": f"{P} | CPF_OutParm | CPF_ReferenceParm"},
                {"name": "Ignored", "type": "ArrayProperty", "inner": {"type": "ObjectProperty", "property_class": "/Script/Engine.Actor"},
                 "flags": f"{P} | CPF_OutParm | CPF_ConstParm | CPF_ReferenceParm"}]},
        "/Game/Things/BP_Plank.BP_Plank_C": {"type": "Class", "super_struct": "/Script/Engine.Actor", "properties": [
            {"name": "HeldBy", "type": "ObjectProperty", "property_class": "/Script/Engine.Actor", "flags": ""},
            {"name": "Speed", "type": "FloatProperty", "flags": ""},
            {"name": "Holders", "type": "MapProperty", "key_prop": {"type": "NameProperty"},
             "value_prop": {"type": "ClassProperty", "meta_class": "/Script/Engine.Actor"}, "flags": ""}]},
        "/Game/Things/BP_Plank.BP_Plank_C:Grab": {"type": "Function", "outer": "/Game/Things/BP_Plank.BP_Plank_C",
            "function_flags": "FUNC_BlueprintCallable | FUNC_BlueprintEvent", "properties": [
                {"name": "Socket", "type": "NameProperty", "flags": P},
                {"name": "CallFunc_Temp", "type": "IntProperty", "flags": "CPF_ZeroConstructor"}]},
        "/Game/Things/BP_Rock.BP_Rock_C:Grab": {"type": "Function", "outer": "/Game/Things/BP_Rock.BP_Rock_C",
            "function_flags": "FUNC_BlueprintEvent", "properties": [{"name": "Strength", "type": "FloatProperty", "flags": P}]},
        "/Game/Things/BP_Rock.BP_Rock_C": {"type": "Class", "super_struct": "/Script/Engine.Actor", "properties": []},
        "/Game/Things/BP_Plank.Default__BP_Plank_C": {"type": "Object", "object_flags": "RF_Public | RF_ClassDefaultObject",
            "class": "/Game/Things/BP_Plank.BP_Plank_C", "property_values": {"Speed": 2.5, "HeldBy": None}},
        "/Script/Engine.EMovementMode": {"type": "Enum", "names": [["MOVE_None", 0], ["MOVE_Walking", 1]]},
    },
}


def make_index(tmp: str) -> Path:
    jmap = Path(tmp) / "MyGame-5.6.0-20702-abc.jmap"
    jmap.write_text(json.dumps(JMAP))
    db = Path(tmp) / index.index_name(jmap.name)
    counts = index.build(str(jmap), str(db))
    assert counts["functions"] == 5, counts
    return db


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_index(self.tmp.name)
        self.ix = index.Index(self.db)

    def tearDown(self):
        self.ix.db.close()
        self.tmp.cleanup()

    def test_meta_and_names(self):
        self.assertEqual(self.ix.meta()["project"], "MyGame")
        self.assertEqual(self.db.name, "MyGame-5.6.0-20702-abc.sqlite")
        self.assertEqual(index.project_of_ue4ss(r"C:\Steam\steamapps\common\My Game\MyGame\Binaries\Win64\ue4ss"), "MyGame")

    def test_search(self):
        rows = self.ix.search("plank")
        self.assertEqual(rows[0], "class /Game/Things/BP_Plank.BP_Plank_C")
        self.assertIn("function /Game/Things/BP_Plank.BP_Plank_C:Grab(FName Socket)  [callable,event]", self.ix.search("grab"))
        self.assertEqual(self.ix.search("speed", kind="property"), ["property /Game/Things/BP_Plank.BP_Plank_C.Speed: float"])
        self.assertIn("nothing named like", self.ix.search("zzz")[0])
        self.assertIn("matches, 1 shown", self.ix.search("a", limit=1)[-1])

    def test_describe(self):
        text = "\n".join(self.ix.describe("BP_Plank"))
        self.assertIn("class /Game/Things/BP_Plank.BP_Plank_C : Actor : Object", text)
        self.assertIn("  Speed: float = 2.5", text)
        self.assertIn("  HeldBy: Actor*\n", text + "\n")
        self.assertIn("  Holders: TMap<FName, TSubclassOf<Actor>>", text)
        self.assertIn("  Grab(FName Socket)  [callable,event]", text)
        self.assertNotIn("ActorHasTag", text)
        self.assertIn("ActorHasTag(FName Tag) -> bool", "\n".join(self.ix.describe("BP_Plank_C", inherited=True)))
        self.assertIn("  MOVE_Walking = 1", "\n".join(self.ix.describe("EMovementMode")))
        self.assertIn("struct /Script/CoreUObject.Vector", self.ix.describe("FVector")[0])

    def test_signature_roles(self):
        text = "\n".join(self.ix.signature("Actor:K2_SetActorLocation"))
        self.assertIn("(Vector NewLocation, bool bSweep, out HitResult SweepHitResult, bool bTeleport) -> bool", text)
        self.assertIn("3. SweepHitResult: HitResult (out: pass {} and read it after the call", text)
        bounds = "\n".join(self.ix.signature("GetActorBounds"))
        self.assertIn("2. Origin: Vector (in and out", bounds)
        self.assertIn("3. Ignored: TArray<Actor*> (in;", bounds, "a const reference is an input")
        self.assertIn("names 2 functions", self.ix.signature("Grab")[0])
        self.assertIn("FName parameters need FName", "\n".join(self.ix.signature("BP_Plank_C:Grab")))
        self.assertIn("ActorHasTag", self.ix.signature("BP_Plank_C:ActorHasTag")[0], "found on a parent class")
        self.assertIn("no function", self.ix.signature("Fly")[0])

    def test_subclasses(self):
        self.assertEqual(self.ix.subclasses("Actor")[0], "2 subclasses of /Script/Engine.Actor")

    def test_check(self):
        src = '''local p = FindFirstOf("BP_Plank_C")
if p:ActorHasTag("Plank") then end -- crash
p:ActorHasTag(FName("Plank")) p:ActorHasTag(tag)
p:K2_SetActorLocation({X = 1}, false, true)
local s = "p:ActorHasTag('in a string')"
--[[ p:ActorHasTag("in a comment") ]]
p:Grab("Hand")
p:Grab(nil)
p:IsValid()
'''
        found = [str(f) for f in check_source(self.ix, src, "m.lua")]
        self.assertEqual(found[0], 'm.lua:2:6: error: ActorHasTag argument 1 (Tag) is an FName: a string here is a native crash in '
                                   'UE4SS, not a Lua error; pass FName("Plank")')
        self.assertIn("m.lua:4:3: warning: K2_SetActorLocation gets 3 arguments; the functions of this name take 4", found[1])
        self.assertIn("m.lua:7:3: warning: Grab argument 1 (Socket) is an FName: a string", found[2])
        self.assertIn("(if this is BP_Plank_C's Grab; 2 functions have this name)", found[2])
        self.assertIn("m.lua:8:3: warning: Grab argument 1 (Socket) is an FName: nil here", found[3])
        self.assertEqual(len(found), 4, found)

    def test_cli_and_mcp(self):
        mod = Path(self.tmp.name) / "MyMod" / "Scripts"
        mod.mkdir(parents=True)
        (mod / "main.lua").write_text('FindFirstOf("Actor"):ActorHasTag("x")\n')
        saved = dict(os.environ)
        os.environ.pop("UVMOD_UE4SS", None)
        os.environ.update({"UVMOD_CONFIG": "/nonexistent", "UVMOD_INDEX": str(self.db)})
        try:
            def run(*argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli.main(list(argv))
                return rc, buf.getvalue()

            rc, out = run("api", "sig", "ActorHasTag")
            self.assertEqual(rc, 0)
            self.assertIn("1. Tag: FName (in; NameProperty)", out)
            self.assertIn("(index: MyGame, UE 5.6 CL 20702", out)
            rc, out = run("check", str(mod.parent))
            self.assertEqual(rc, 1)
            self.assertIn("main.lua:1:22: error: ActorHasTag argument 1 (Tag)", out)
            server = Server()

            def tool(**arguments):
                return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                      "params": {"name": "api", "arguments": arguments}})["result"]

            r = tool(action="search", text="plank", kind="class")
            self.assertIn("class /Game/Things/BP_Plank.BP_Plank_C", r["content"][0]["text"])
            r = tool(action="check", path=str(mod))
            self.assertTrue(r["isError"])
            self.assertIn("1 errors", r["content"][0]["text"])
            os.environ["UVMOD_INDEX"] = str(Path(self.tmp.name) / "none.sqlite")
            self.assertEqual(run("api", "search", "x")[0], 2)
        finally:
            os.environ.clear()
            os.environ.update(saved)


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ix = index.Index(make_index(self.tmp.name))

    def tearDown(self):
        self.ix.db.close()
        self.tmp.cleanup()

    def test_new_and_mock(self):
        from uvmod import scaffold

        target = scaffold.new("MyMod", self.tmp.name)
        for rel in ("MyMod/Scripts/main.lua", "MyMod/Scripts/logic.lua", "MyMod/enabled.txt", "tests/ue4ss.lua",
                    "tests/test_mod.lua", "tests/run.sh", "tests/game_api.lua", "README.md", ".gitignore"):
            self.assertTrue((target / rel).exists(), rel)
        self.assertNotIn("{{NAME}}", (target / "MyMod/Scripts/main.lua").read_text())
        self.assertIn('local NAME = "MyMod"', (target / "MyMod/Scripts/main.lua").read_text())
        self.assertTrue(os.access(target / "tests/run.sh", os.X_OK))
        with self.assertRaises(ValueError):
            scaffold.new("MyMod", self.tmp.name)
        with self.assertRaises(ValueError):
            scaffold.new("1bad", self.tmp.name)
        out, written, unknown = scaffold.mock(str(target), self.ix)
        self.assertEqual(written, 0, "the example calls nothing this small index has")
        self.assertIn("IsLocalController", unknown)
        self.assertNotIn("close", unknown, "Lua's own lowercase methods are not UFunctions")
        if not HAVE_LUA:
            return
        import subprocess

        from test_bridge_e2e import LUA

        run = lambda: subprocess.run([LUA, "tests/test_mod.lua"], cwd=target, capture_output=True, text=True)
        done = run()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("5 passed, 0 failed", done.stdout)
        # The stand-in enforces the real signature once `mock` has written it: a string for an FName crashes.
        logic = target / "MyMod/Scripts/logic.lua"
        logic.write_text(logic.read_text().replace("local loc = pawn:K2_GetActorLocation()",
                                                   'pawn:ActorHasTag("Player")\n        local loc = pawn:K2_GetActorLocation()'))
        test = target / "tests/test_mod.lua"
        test.write_text(test.read_text().replace("methods = { K2_GetActorLocation", "methods = { ActorHasTag = function() return true end, K2_GetActorLocation"))
        out, written, unknown = scaffold.mock(str(target), self.ix)
        self.assertEqual(written, 1)
        self.assertIn('ActorHasTag = {', out.read_text())
        done = run()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("NATIVE CRASH", done.stdout + done.stderr)
        self.assertIn("ActorHasTag argument 1 (Tag) is an FName, got string", done.stdout + done.stderr)


@unittest.skipUnless(HAVE_LUA, "needs Lua 5.4 (set LUA=/path/to/lua)")
class IndexEndToEnd(BridgeHarness, unittest.TestCase):
    def test_index_from_the_running_game(self):
        self.start()
        data = tempfile.TemporaryDirectory()
        saved = os.environ.get("XDG_DATA_HOME")
        os.environ["XDG_DATA_HOME"] = data.name
        try:
            report = self.game.build_index()
            self.assertIn("FakeGame-5.6.0-1-fake.jmap", report)
            self.assertIn("1 functions", report)
            db = index.find("", str(self.dir.ue4ss))
            self.assertIsNotNone(db)
            self.assertFalse(list(Path(data.name, "uvmod", "index").glob("*.jmap")), "the copied dump is removed")
            self.assertIn("Tag: FName", "\n".join(index.Index(db).signature("ActorHasTag")))
        finally:
            if saved is None:
                os.environ.pop("XDG_DATA_HOME", None)
            else:
                os.environ["XDG_DATA_HOME"] = saved
            data.cleanup()


if __name__ == "__main__":
    unittest.main()
