"""The watch helper end to end: the real lua/watch.lua inside the real VibeBridge (tests/fake_ue4ss.lua stands in
for UE4SS's RegisterHook), driven by the real client, CLI and MCP server."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_bridge_e2e import HAVE_LUA, BridgeHarness  # noqa: E402
from test_client import ROOT  # noqa: E402
from uvmod import cli  # noqa: E402
from uvmod.game import GameError  # noqa: E402
from uvmod.watch import LuaError, Watcher, lua_string, render_events, render_watches  # noqa: E402

GRAB = "/Game/Fake/BP_Plank.BP_Plank_C:Grab"
NATIVE = "/Script/Engine.Actor:K2_DestroyActor"


@unittest.skipUnless(HAVE_LUA, "needs Lua 5.4 (set LUA=/path/to/lua)")
class WatchEndToEnd(BridgeHarness, unittest.TestCase):
    def setUp(self):
        self.start()
        self.w = Watcher(self.game)

    def lua(self, code):
        r = self.game.exec(code)
        self.assertIsNotNone(r.answer, code)
        self.assertTrue(r.answer["ok"], r.answer.get("error"))
        return r.answer["values"]

    def fire(self, *args):
        self.lua(f"FakeFire({', '.join(args)})")

    def test_lua_string(self):
        for text in ['plain', 'quote " backslash \\ ', 'tab\t newline\n cr\r nul\x00 bell\x07', 'h\u00e9 \u2603 \U0001f600', '"]==]"']:
            self.assertEqual(self.lua(f"return {lua_string(text)}"), ['"' + text + '"'], repr(text))

    def test_watch_events(self):
        batch = self.w.watch(GRAB)
        self.assertEqual(batch.active, ["Grab"])
        self.assertIn("Grab: " + GRAB + " (pre) 0 calls, 0/200 recorded", render_watches(batch))
        self.assertEqual(self.w.events().events, [])
        self.fire(lua_string(GRAB), '"Plank_3"')
        b = self.w.events()
        self.assertEqual([(e.seq, e.watch, e.text) for e in b.events], [(1, "Grab", "self=Plank_3")])
        self.assertEqual(self.w.events().events, [], "events are read once")
        self.lua(f"for i = 1, 5 do FakeFire({lua_string(GRAB)}, 'Plank_3') end FakeFire({lua_string(GRAB)}, 'Plank_4')")
        text = render_events(self.w.events())
        self.assertIn("6 new events (cursor 7; watching: Grab)", text)
        self.assertIn("#2-#6 ", text)
        self.assertIn("Grab: self=Plank_3 (x5)", text)
        self.assertIn("#7 ", text)
        self.assertEqual(self.w.events(since=0).cursor, 7, "an explicit cursor reads again")

    def test_capture(self):
        self.w.watch(GRAB, "args", capture="select(1, ...), select(2, ...), select(3, ...)")
        self.fire(lua_string(GRAB), '"P"', "1.5", 'FakeObject("Hand")', "{Y = 2, X = 1, 'first'}")
        self.w.watch(GRAB, "skip", capture="select(1, ...) > 1 and self or nil")
        self.w.watch(GRAB, "dead", capture="FakeObject('Gone', false), nil, true, 3.0, 7")
        self.fire(lua_string(GRAB), '"Q"', "0")
        self.fire(lua_string(GRAB), '"R"', "5")
        events = [(e.watch, e.text) for e in self.w.events().events]
        self.assertIn(("args", '1.5, Hand, {"first", X=1, Y=2}'), events)
        self.assertEqual([t for w, t in events if w == "skip"], ["R"])
        self.assertIn(("dead", "(invalid), nil, true, 3, 7"), events)

    def test_capture_errors_and_limit(self):
        self.w.watch(GRAB, "bad", capture="self.no.such.field")
        self.w.watch(GRAB, "few", limit=3)
        self.lua(f"for i = 1, 5 do FakeFire({lua_string(GRAB)}, 'P' .. i) end")
        b = self.w.events()
        bad = [e.text for e in b.events if e.watch == "bad"]
        self.assertEqual(len(bad), 3)
        self.assertTrue(bad[0].startswith("capture error: "), bad[0])
        self.assertEqual([e.text for e in b.events if e.watch == "few"], ["self=P1", "self=P2", "self=P3"])
        text = render_events(b)
        self.assertIn("watch few stopped recording at its limit of 3 (5 calls so far)", text)
        self.assertIn("watch bad: 5 capture error(s), last: ", text)

    def test_post_unwatch_and_errors(self):
        self.w.watch(NATIVE, "after", post=True)
        self.w.watch(GRAB, "before")
        self.assertEqual(self.lua("FakeHookCount()"), ["2"])
        self.fire(lua_string(NATIVE), '"P"')
        self.fire(lua_string(GRAB), '"P"')
        self.assertEqual(sorted(e.watch for e in self.w.events().events), ["after", "before"])
        with self.assertRaises(LuaError) as e:
            self.w.watch(GRAB, "bp-post", post=True)
        self.assertIn("does not run post callbacks for Blueprint functions", str(e.exception))
        self.assertEqual(self.lua("FakeHookCount()"), ["2"], "the refused hook is unregistered")
        self.w.watch(GRAB, "before")  # the same id again replaces the watch
        self.assertEqual(self.lua("FakeHookCount()"), ["2"])
        self.assertEqual(self.w.unwatch("after").active, ["before"])
        self.lua("__uvmod.version = 0")  # an older helper in the game: the next call replaces it, unhooking first
        self.assertEqual(self.w.list().active, [])
        self.assertEqual(self.lua("FakeHookCount()"), ["0"])
        self.w.watch(GRAB, "before")
        self.assertEqual(self.w.unwatch("not-there").active, ["before"])
        self.assertEqual(self.w.unwatch().active, [])
        self.assertEqual(self.lua("FakeHookCount()"), ["0"])
        with self.assertRaises(LuaError) as e:
            self.w.watch(GRAB, capture="self..")
        self.assertIn("the capture does not compile", str(e.exception))
        with self.assertRaises(LuaError) as e:
            self.w.watch("NoColonHere")
        self.assertIn("RegisterHook failed: ", str(e.exception))
        with self.assertRaises(ValueError):
            self.w.watch(GRAB, "bad id")

    def test_overflow_and_paging(self):
        self.w.watch(GRAB, limit=5000)
        self.lua("__uvmod.max = 5")
        self.lua(f"for i = 1, 8 do FakeFire({lua_string(GRAB)}, 'P' .. i) end")
        b = self.w.events()
        self.assertEqual((b.lost, [e.text for e in b.events]), (3, ["self=P4", "self=P5", "self=P6", "self=P7", "self=P8"]))
        self.assertIn("(3 older events were dropped", render_events(b))
        self.lua("__uvmod.max = 1000")
        self.lua(f"for i = 1, 250 do FakeFire({lua_string(GRAB)}, 'P' .. i) end")
        b = self.w.events()
        self.assertEqual((len(b.events), b.pending), (200, 50))
        self.assertIn("(50 more events waiting: call events again)", render_events(b))
        b = self.w.events()
        self.assertEqual((len(b.events), b.pending, b.events[-1].text), (50, 0, "self=P250"))

    def test_wait(self):
        self.w.watch(GRAB)
        self.lua(f"FakeLater(300, function() FakeFire({lua_string(GRAB)}, 'Late') end)")
        met, batch, seconds = self.w.wait("Grab", timeout=5)
        self.assertTrue(met)
        self.assertGreater(seconds, 0.15)
        self.assertEqual([e.text for e in batch.events], ["self=Late"])
        met, batch, seconds = self.w.wait(timeout=0.4)
        self.assertFalse(met)
        self.assertGreaterEqual(seconds, 0.4)
        with self.assertRaises(GameError):
            self.w.wait("Nope", timeout=1)
        self.lua("Flag = false")
        self.lua("FakeLater(300, function() Flag = 'up' end)")
        self.assertEqual(self.w.wait(until="Flag", timeout=5)[:2], (True, '"up"'))
        self.assertEqual(self.w.wait(until="Flag == 'down'", timeout=0.3)[:2], (False, "false"))
        with self.assertRaises(LuaError):
            self.w.wait(until="nil + 1", timeout=1)

    def test_restart_is_noticed(self):
        self.w.watch(GRAB)
        self.lua("__uvmod = nil")  # what a mod restart leaves: a fresh Lua state
        with self.assertRaises(GameError) as e:
            self.w.wait("Grab", timeout=1)
        self.assertIn("nothing is being watched", str(e.exception))

    def test_cli_and_mcp(self):
        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli.main(list(argv))
            return rc, out.getvalue()

        self.assertEqual(run("watch", GRAB, "--id", "g")[0], 0)
        self.fire(lua_string(GRAB), '"P"')
        rc, out = run("events")
        self.assertEqual(rc, 0)
        self.assertIn("g: self=P", out)
        rc, out = run("wait", "--for", "g", "--timeout", "0.3")
        self.assertEqual(rc, 4)
        self.assertIn("TIMEOUT after", out)
        self.assertIn("g: " + GRAB, run("watches")[1])
        self.assertIn("ERROR: the capture does not compile", run("watch", GRAB, "--capture", "((")[1])
        self.assertEqual(run("watch", GRAB, "--capture", "((")[0], 1)
        self.assertEqual(run("unwatch")[1], "watching: nothing\n")

        env = dict(os.environ, PYTHONPATH=str(ROOT))
        proc = subprocess.Popen([sys.executable, "-m", "uvmod", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, env=env, cwd=str(ROOT))

        def rpc(msg_id, name, arguments):
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                                         "params": {"name": name, "arguments": arguments}}) + "\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())["result"]

        try:
            r = rpc(1, "watch", {"function": GRAB, "capture": "select(1, ...)"})
            self.assertFalse(r["isError"], r)
            self.assertIn("watching: Grab", r["content"][0]["text"])
            self.lua(f"FakeLater(200, function() FakeFire({lua_string(GRAB)}, 'P', 42) end)")
            r = rpc(2, "wait", {"for": "Grab", "timeout_s": 5})
            self.assertIn("Grab: 42", r["content"][0]["text"])
            self.assertTrue(r["content"][0]["text"].startswith("seen after "))
            r = rpc(3, "events", {})
            self.assertTrue(r["content"][0]["text"].startswith("no new events"))
            r = rpc(4, "watch", {"function": "NoColon"})
            self.assertTrue(r["isError"])
            self.assertIn("ERROR: RegisterHook failed", r["content"][0]["text"])
            r = rpc(5, "unwatch", {})
            self.assertEqual(r["content"][0]["text"], "watching: nothing")
        finally:
            proc.stdin.close()
            proc.wait(10)
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
