"""The inspect helper end to end: the real lua/inspect.lua inside the real VibeBridge, against the fake reflection in
tests/fake_reflection.lua (which dies like the real game when an FName parameter gets anything but an FName), driven
by the real client, CLI and MCP server."""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bridge_e2e import HAVE_LUA, LUA, BridgeHarness  # noqa: E402
from uvmod import cli, helpers  # noqa: E402
from uvmod.mcp import Server  # noqa: E402
from uvmod.reflection import Inspector, parse_cli_args, render  # noqa: E402
from uvmod.watch import LuaError  # noqa: E402

PLANK = "first:BP_Plank_C"


class Pure(unittest.TestCase):
    def test_lua_value(self):
        self.assertEqual(helpers.lua_args([None, True, 2, 2.5, "a\"b", [1, "x"], {"X": 1, "two words": 2}]),
                         'nil, true, 2, 2.5, "a\\"b", {1, "x"}, {X = 1, ["two words"] = 2}')
        with self.assertRaises(ValueError):
            helpers.lua_value(object())

    def test_cli_args(self):
        self.assertEqual(parse_cli_args(["2.5", "true", '{"X": 1}', "RightHand", "first:Pawn"]),
                         [2.5, True, {"X": 1}, "RightHand", "first:Pawn"])

    def test_minified_helpers_compile_and_fit(self):
        for h in (helpers.WATCH, helpers.INSPECT):
            self.assertLess(len(h.payload.encode()), 40000, h.filename)
            self.assertNotIn("\n--", h.payload)
            if HAVE_LUA:
                done = subprocess.run([LUA, "-e", "assert(load(io.read('a')))"], input=h.payload.encode(), capture_output=True)
                self.assertEqual(done.returncode, 0, done.stderr.decode())


@unittest.skipUnless(HAVE_LUA, "needs Lua 5.4 (set LUA=/path/to/lua)")
class InspectEndToEnd(BridgeHarness, unittest.TestCase):
    def setUp(self):
        self.start()
        self.i = Inspector(self.game)

    def alive(self):
        r = self.game.exec("1 + 1")
        self.assertIsNotNone(r.answer, "the fake game died")
        return True

    def test_world_find(self):
        text = render("world", self.i.world())
        self.assertIn("pawn: Pawn /Game/Maps/Test.Test:PersistentLevel.BP_Hero_C_0", text)
        self.assertIn("pawn location: (X=10 Y=20 Z=30)", text)
        found = self.i.find("all:Actor")
        self.assertEqual(len(found.of("X")), 4)
        self.assertEqual(self.i.find("all:Actor", limit=2).total, 4)
        self.assertIn("BP_Plank_C_3", render("find", self.i.find(PLANK)))
        with self.assertRaisesRegex(LuaError, "no live instance of Nope"):
            self.i.find("first:Nope")
        with self.assertRaisesRegex(LuaError, "unknown reference"):
            self.i.find("Plank")

    def test_props_and_functions(self):
        text = render("props", self.i.props(PLANK))
        self.assertIn("BP_Plank_C.Offset: Vector = (X=1 Y=2 Z=3)", text)
        self.assertIn('BP_Plank_C.Label: FString = "a plank"', text)
        self.assertIn("BP_Plank_C.HeldBy: Pawn* = nil", text)
        self.assertIn("BP_Plank_C.Pickup: TSoftObjectPtr<Object> = (not read: SoftObjectProperty)", text)
        self.assertIn("Actor.Tags: TArray<FName>", text)
        own = render("props", self.i.props(PLANK, inherited=False))
        self.assertNotIn("bHidden", own)
        self.assertEqual([f[1] for f in self.i.props("pawn", "health").of("P")], ["Health"])
        self.assertIn("Pawn.MoveMode: EMovementMode = 1", render("props", self.i.props("pawn")))
        self.assertIn("Actor.bHidden: bool\n", render("props", self.i.props("class:/Script/Engine.Pawn")) + "\n")

        funcs = render("functions", self.i.functions(PLANK, "grab"))
        self.assertIn("BP_Plank_C::Grab(Pawn* Who, FName Socket)  [native,callable]", funcs)
        self.assertIn("Actor::ActorHasTag(FName Tag) -> bool", render("functions", self.i.functions(PLANK)))
        sig = render("signature", self.i.signature(PLANK, "Grab"))
        self.assertIn("2. Socket: FName (in, NameProperty)", sig)
        self.assertNotIn("CallFunc_Temp", sig, "Blueprint locals are not parameters")
        with self.assertRaisesRegex(LuaError, "no function Fly"):
            self.i.signature(PLANK, "Fly")

    def test_guarded_call(self):
        out = render("call", self.i.call(PLANK, "Grab", ["pawn", "RightHand"]))
        self.assertIn("called BP_Plank_C::Grab(Pawn* Who, FName Socket)", out)
        self.assertIn('"held at RightHand"', render("props", self.i.props(PLANK, "label")))
        self.assertIn("BP_Hero_C_0", render("props", self.i.props(PLANK, "heldby")))
        self.assertIn("=> true", render("call", self.i.call(PLANK, "ActorHasTag", ["Plank"])))
        self.assertIn("=> false", render("call", self.i.call(PLANK, "ActorHasTag", lua_args='FName("Rock")')))

        for args, why in ((["pawn", None], "is an FName: pass text, not nil"),
                          (["pawn", 5], "is an FName: pass text, not number"),
                          (["pawn"], "is an FName and missing"),
                          (["first:Nope", "Hand"], "no live instance of Nope")):
            with self.assertRaisesRegex(LuaError, "REFUSED Grab"):
                try:
                    self.i.call(PLANK, "Grab", args)
                except LuaError as e:
                    self.assertIn(why, str(e))
                    raise
            self.alive()
        with self.assertRaisesRegex(LuaError, "is a Vector struct: pass a table, not number"):
            self.i.call(PLANK, "Nudge", [5])
        with self.assertRaisesRegex(LuaError, "expected 1 parameters, received 2"):
            self.i.call(PLANK, "Nudge", [{"X": 1, "Y": 0, "Z": 0}, 1])
        with self.assertRaises(ValueError):
            self.i.call(PLANK, "Nudge", [1], lua_args="1")
        self.alive()

    def test_unguarded_string_for_fname_kills_the_fake(self):
        # The control for the guard: the same mistake without it ends the (fake) game, as it does the real one.
        r = self.game.exec('local p = FindFirstOf("BP_Plank_C") return p:Grab(FindFirstOf("Pawn"), "RightHand")')
        self.assertIsNone(r.answer)
        self.proc.wait(5)
        self.assertIn("NATIVE CRASH: Grab got string for FName parameter Socket", self.log())

    def test_snapshot_diff(self):
        self.assertIn("snapshot before:", render("snapshot", self.i.snapshot("before", PLANK)))
        self.assertEqual(render("diff", self.i.diff("before")).splitlines()[-1], "nothing changed")
        self.i.call(PLANK, "Nudge", [{"X": 1, "Y": 0, "Z": 0}])
        self.i.call(PLANK, "SetActorHiddenInGame", [True])
        text = render("diff", self.i.diff("before", update=True))
        self.assertIn("2 properties changed", text)
        self.assertIn("Offset: (X=1 Y=2 Z=3) -> (X=2 Y=2 Z=3)", text)
        self.assertIn("bHidden: false -> true", text)
        self.assertIn("nothing changed", render("diff", self.i.diff("before")))
        self.i.snapshot("hp", "pawn", "health")
        self.assertEqual(len(self.i.snapshots().of("S")), 2)
        self.assertEqual(len(self.i.forget("hp").of("S")), 1)
        self.assertEqual(render("snapshots", self.i.forget()), "no snapshots")
        with self.assertRaisesRegex(LuaError, "no snapshot named before"):
            self.i.diff("before")

    def test_classes_and_target(self):
        names = [f[0] for f in self.i.classes(base="/Script/Engine.Actor").of("K")]
        self.assertEqual(sorted(names), ["Actor", "BP_Plank_C", "Pawn", "PlayerCameraManager", "PlayerController"])
        self.assertEqual([f[0] for f in self.i.classes("plank").of("K")], ["BP_Plank_C"])
        hit = render("target", self.i.target(5000))
        self.assertIn("actor: BP_Plank_C /Game/Maps/Test.Test:PersistentLevel.BP_Plank_C_3", hit)
        self.assertIn("Distance: 250.0", hit)
        self.assertIn("nothing hit within 500 cm", render("target", self.i.target(500)))

    def test_helper_is_sent_once(self):
        sizes = []
        real = self.game.exec

        def spy(code, mod=None, limit=None):
            sizes.append(len(code))
            return real(code, mod, limit)

        self.game.exec = spy
        self.i.world()
        self.i.world()
        self.assertEqual(len(sizes), 3, "check, then helper + call, then just the call")
        self.assertGreater(sizes[1], 10000)
        self.assertLess(sizes[2], 500)

    def test_cli(self):
        def run(*argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(list(argv))
            return rc, buf.getvalue()

        rc, out = run("props", PLANK, "off")
        self.assertEqual(rc, 0)
        self.assertIn("Offset: Vector = (X=1 Y=2 Z=3)", out)
        rc, out = run("call", PLANK, "Nudge", '{"X": 0, "Y": 0, "Z": 5}')
        self.assertEqual(rc, 0, out)
        rc, out = run("call", PLANK, "Grab", "pawn")
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", out)
        rc, out = run("call", PLANK, "ActorHasTag", "--lua", 'FName("Plank")')
        self.assertIn("=> true", out)
        rc, out = run("snap", "a", PLANK)
        rc, out = run("diff", "a")
        self.assertIn("nothing changed", out)
        rc, out = run("classes", "plank", "--kind", "class")
        self.assertIn("/Game/Fake/BP_Plank.BP_Plank_C", out)

    def test_mcp(self):
        server = Server()
        server.game = self.game
        names = {t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
        self.assertTrue({"inspect", "call", "snapshot"} <= names)

        def tool(name, **arguments):
            reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
            return reply["result"]

        r = tool("inspect", what="props", ref=PLANK, pattern="label")
        self.assertFalse(r["isError"])
        self.assertIn('Label: FString = "a plank"', r["content"][0]["text"])
        r = tool("call", ref=PLANK, function="Grab", args=["pawn"])
        self.assertTrue(r["isError"])
        self.assertIn("REFUSED", r["content"][0]["text"])
        r = tool("call", ref=PLANK, function="Grab", args=["pawn", "LeftHand"])
        self.assertFalse(r["isError"], r)
        r = tool("snapshot", action="take", label="m", ref="pawn")
        self.assertIn("snapshot m:", r["content"][0]["text"])
        r = tool("snapshot", action="diff", label="m")
        self.assertIn("nothing changed", r["content"][0]["text"])
        r = tool("inspect", what="nope")
        self.assertTrue(r["isError"])


if __name__ == "__main__":
    unittest.main()
