"""Starting, stopping and crash triage: the journal of sent requests, the minidump and CrashContext readers, Steam app
id discovery, the crash report from a crash folder, and launch/stop/ready on the local transport."""

from __future__ import annotations

import io
import os
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_game import FakeGame  # noqa: E402
from test_client import GameDir  # noqa: E402
from uvmod import cli, journal  # noqa: E402
from uvmod.config import Settings  # noqa: E402
from uvmod.crash import parse_crash_context, parse_minidump  # noqa: E402
from uvmod.game import Game  # noqa: E402
from uvmod.mcp import Server  # noqa: E402
from uvmod.transports.local import LocalTransport  # noqa: E402

GAME_BASE, UE4SS_BASE = 0x7FF600000000, 0x7FFA00000000


def build_minidump(code=0xC0000005, address=UE4SS_BASE + 0x2B56B3, info=(0, 0x10), rsp=0x5000_0000,
                   stack=(0x1234, UE4SS_BASE + 0x2B5700, GAME_BASE + 0x14B420C, 0x99), memory64=False) -> bytes:
    """A minimal minidump: two modules, an exception with a context, and the crashing stack's memory."""
    modules = [("C:\\Game\\Binaries\\Win64\\Game-Win64-Shipping.exe", GAME_BASE, 0x5000000),
               ("C:\\Game\\Binaries\\Win64\\ue4ss\\UE4SS.dll", UE4SS_BASE, 0x3000000)]
    streams = 4
    out = bytearray(32 + 12 * streams)
    struct.pack_into("<4sIII", out, 0, b"MDMP", 0xA793, streams, 32)
    directory = []

    def add(blob: bytes) -> int:
        rva = len(out)
        out.extend(blob)
        return rva

    names = []
    for name, _, _ in modules:
        enc = name.encode("utf-16-le")
        names.append(add(struct.pack("<I", len(enc)) + enc))
    mods = struct.pack("<I", len(modules))
    for (_, base, size), name_rva in zip(modules, names):
        entry = bytearray(108)
        struct.pack_into("<QIIII", entry, 0, base, size, 0, 0, name_rva)
        mods += bytes(entry)
    directory.append((4, len(mods), add(mods)))

    context = bytearray(0x4D0)
    struct.pack_into("<Q", context, 0x98, rsp)
    struct.pack_into("<Q", context, 0xF8, address)
    ctx_rva = add(bytes(context))
    exc = bytearray(168)
    struct.pack_into("<IIIIQQI", exc, 0, 7, 0, code, 0, 0, address, len(info))
    struct.pack_into(f"<{len(info)}Q", exc, 40, *info)
    struct.pack_into("<II", exc, 160, len(context), ctx_rva)
    directory.append((6, len(exc), add(bytes(exc))))

    stack_bytes = b"".join(struct.pack("<Q", v) for v in stack)
    if memory64:
        data_rva = len(out) + 16 + 16
        mem = struct.pack("<QQ", 1, data_rva) + struct.pack("<QQ", rsp, len(stack_bytes))
        directory.append((9, len(mem), add(mem)))
        add(stack_bytes)
    else:
        data_rva = add(stack_bytes)
        mem = struct.pack("<I", 1) + struct.pack("<QII", rsp, len(stack_bytes), data_rva)
        directory.append((5, len(mem), add(mem)))
    directory.append((7, 0, 0))
    for i, (kind, size, rva) in enumerate(directory):
        struct.pack_into("<III", out, 32 + 12 * i, kind, size, rva)
    return bytes(out)


CONTEXT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<FGenericCrashContext><RuntimeProperties>
<ErrorMessage>Unhandled Exception: EXCEPTION_ACCESS_VIOLATION reading address 0x0000000000000010</ErrorMessage>
<EngineVersion>5.6.0-20702</EngineVersion>
<CallStack>UE4SS!RC::LuaType::push_nameproperty() [LuaUObject.cpp:640]
UE4SS!RC::LuaType::call_ufunction_from_lua()
Game_Win64_Shipping</CallStack>
<Modules>C:\\Game\\Binaries\\Win64\\Game-Win64-Shipping.exe
C:\\Game\\Binaries\\Win64\\ue4ss\\UE4SS.dll</Modules>
</RuntimeProperties></FGenericCrashContext>"""


class Readers(unittest.TestCase):
    def test_minidump(self):
        for memory64 in (False, True):
            dump = parse_minidump(build_minidump(memory64=memory64))
            self.assertEqual(dump.code, 0xC0000005)
            self.assertEqual(dump.where(dump.address), ("UE4SS.dll", 0x2B56B3))
            self.assertEqual(dump.stack, [("UE4SS.dll", 0x2B5700), ("Game-Win64-Shipping.exe", 0x14B420C)])
            text = "\n".join(dump.describe())
            self.assertIn("exception 0xC0000005 (access violation) at UE4SS.dll+0x2B56B3", text)
            self.assertIn("reading address 0x10  <- a null pointer", text)
            self.assertIn("  UE4SS.dll+0x2B5700", text)

    def test_minidump_damaged(self):
        self.assertIn("not a minidump", parse_minidump(b"PK\x03\x04junk").describe()[0])
        outside = parse_minidump(build_minidump(address=0x790072, info=(8, 0x790072)))
        self.assertIn("at 0x790072 (outside every module)", outside.describe()[0])
        self.assertIn("executing address 0x790072", outside.describe()[1])

    def test_crash_context(self):
        ctx = parse_crash_context(CONTEXT_XML)
        self.assertIn("EXCEPTION_ACCESS_VIOLATION", ctx.error)
        self.assertEqual(ctx.engine, "5.6.0-20702")
        self.assertEqual(ctx.callstack[0], "UE4SS!RC::LuaType::push_nameproperty() [LuaUObject.cpp:640]")
        self.assertTrue(ctx.has_ue4ss)
        self.assertEqual(parse_crash_context("not xml").error, "")


class Keys(unittest.TestCase):
    def test_parse(self):
        from uvmod import keys

        self.assertEqual(keys.parse("F8"), [0x77])
        self.assertEqual(keys.parse("control+f8"), [0x11, 0x77])
        self.assertEqual(keys.parse("NUM_ONE"), [0x61])
        self.assertEqual(keys.parse("Q"), [0x51])
        with self.assertRaises(ValueError):
            keys.parse("F25")
        text = keys.script("Game-Win64-Shipping", [0x11, 0x77], 150, r"C:\t\out")
        self.assertLess(text.index("keybd_event(17, 0, 0"), text.index("keybd_event(119, 0, 0"), "modifiers go down first")
        self.assertLess(text.index("keybd_event(119, 0, 2"), text.index("keybd_event(17, 0, 2"), "and come up last")
        self.assertIn("Start-Sleep -Milliseconds 150", text)

    def test_press_key_reports_the_window_in_front(self):
        from uvmod.inputs import press_key

        class FakeTransport:
            front = "UnrealWindow"

            def press_keys(self, codes, hold_ms):
                self.pressed = (codes, hold_ms)
                return self.front

        class FakeGame:
            transport = FakeTransport()

        g = FakeGame()
        self.assertEqual(press_key(g, "F8"), "pressed F8 in the game's window for 150 ms")
        self.assertEqual(g.transport.pressed, ([0x77], 150))
        g.transport.front = "Chrome_WidgetWin_1"
        self.assertIn("not the game's", press_key(g, "F8"))


class Journal(unittest.TestCase):
    def setUp(self):
        self.dir = GameDir()
        self.fake = FakeGame(self.dir.folder, "native")
        self.fake.start()
        self.settings = self.dir.settings()

    def tearDown(self):
        self.fake.stop()
        self.dir.close()

    def test_requests_are_recorded_with_their_outcome(self):
        game = Game(self.settings)
        game.exec("1 + 1", "Alpha")
        game.exec("error('x')", "Alpha")
        self.fake.stop()
        Game(self.dir.settings(UVMOD_TIMEOUT="0.3")).exec("hang", "Alpha")
        entries = journal.tail(self.settings, 10)
        self.assertEqual([(e["code"], e["done"]) for e in entries], [("1 + 1", "ok"), ("error('x')", "error"), ("hang", "timeout")])
        self.assertIn("boom", entries[1]["error"])
        text = journal.render(entries)
        self.assertIn("exec in Alpha: timeout", text)
        self.assertEqual(journal.tail(self.settings, 1)[0]["code"], "hang")

    def test_unanswered_and_long_code(self):
        journal.record(self.settings, {"id": "a1", "op": "exec", "mod": "M", "code": journal.short_code("x" * 5000 + "TAIL")})
        e = journal.tail(self.settings, 1)[0]
        self.assertNotIn("done", e)
        self.assertTrue(e["code"].endswith("TAIL"))
        self.assertIn("characters)...", e["code"])
        self.assertIn("exec in M: no answer", journal.render([e]))

    def test_off(self):
        s = self.dir.settings(UVMOD_JOURNAL="off")
        self.assertIsNone(journal.path(s))
        Game(s).exec("1 + 1", "Alpha")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ue4ss = root / "lib" / "steamapps" / "common" / "My Game" / "MyGame" / "Binaries" / "Win64" / "ue4ss"
        (self.ue4ss / "Mods").mkdir(parents=True)
        (self.ue4ss.parent / "MyGame-Win64-Shipping.exe").write_bytes(b"")
        (self.ue4ss / "UE4SS.log").write_text("[x] UE4SS - v3.0.1\n[x] [ExternalLuaRepl] Request 1 for mod 'A'\n[x] last real line\n")
        (root / "lib" / "steamapps" / "appmanifest_1.acf").write_text('"AppState"\n{\n "appid" "1"\n "installdir" "Other"\n}\n')
        (root / "lib" / "steamapps" / "appmanifest_3949040.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"3949040"\n\t"installdir"\t\t"My Game"\n}\n')
        self.appdata = root / "appdata"
        self.saved_env = dict(os.environ)
        os.environ["LOCALAPPDATA"] = str(self.appdata)
        self.env = {"UVMOD_UE4SS": str(self.ue4ss), "UVMOD_TRANSPORT": "local", "UVMOD_TIMEOUT": "1",
                    "UVMOD_CONFIG": "/nonexistent", "UVMOD_JOURNAL": str(root / "journal.jsonl")}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        self.tmp.cleanup()

    def settings(self, **kw) -> Settings:
        return Settings.load(dict(self.env, **kw))

    def test_appid(self):
        self.assertEqual(LocalTransport(self.settings()).appid(), "3949040")
        self.assertEqual(LocalTransport(self.settings(UVMOD_APPID="42")).appid(), "42")

    def test_launch_command_and_stop(self):
        marker = Path(self.tmp.name) / "launched"
        game = Game(self.settings(UVMOD_LAUNCH=f'"{sys.executable}" -c "open(r\'{marker}\', \'w\').write(\'x\')"'))
        self.assertTrue(game.launch().startswith("started: "))
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        self.assertEqual(game.stop(), "MyGame-Win64-Shipping is not running")

    def test_ready(self):
        game = Game(self.settings())
        ok, text = game.ready(timeout=0.5)
        self.assertFalse(ok)
        self.assertIn("NOT READY", text)
        fake = FakeGame(self.ue4ss / "LuaRepl", "native")
        fake.start()
        try:
            ok, text = game.ready(timeout=10)
            self.assertTrue(ok, text)
            self.assertIn("UE4SS external REPL; mods: Alpha, Beta", text)
        finally:
            fake.stop()

    def test_crash_report(self):
        game = Game(self.settings())
        self.assertIn("no Unreal crash folder", game.crash_report())
        folder = self.appdata / "MyGame" / "Saved" / "Crashes" / "UECC-Windows-ABC_0000"
        folder.mkdir(parents=True)
        (folder / "CrashContext.runtime-xml").write_text(CONTEXT_XML)
        (folder / "UEMinidump.dmp").write_bytes(build_minidump())
        (self.ue4ss / "crash_2026_10_24_12_00_00.dmp").write_bytes(build_minidump(address=GAME_BASE + 0x10))
        journal.record(game.settings, {"id": "q1", "op": "exec", "mod": "A", "code": 'p:Grab(pawn, "Hand")'})
        text = game.crash_report()
        self.assertIn("game: not running", text)
        self.assertIn("newest Unreal crash folder (0 min ago)", text)
        self.assertIn("error: Unhandled Exception: EXCEPTION_ACCESS_VIOLATION", text)
        self.assertIn("UE4SS.dll loaded: yes", text)
        self.assertIn("UE4SS!RC::LuaType::push_nameproperty()", text)
        self.assertIn("exception 0xC0000005 (access violation) at UE4SS.dll+0x2B56B3", text)
        self.assertIn("UE4SS crash dump (0 min ago)", text)
        self.assertIn("at Game-Win64-Shipping.exe+0x10", text)
        self.assertIn("last real line", text)
        self.assertNotIn("Request 1 for mod", text, "the REPL's per-request lines are left out")
        self.assertIn('exec in A: no answer', text)
        self.assertIn('p:Grab(pawn, "Hand")', text)
        self.assertIn("llvm-symbolizer", text)

        buf = io.StringIO()
        for k, v in self.env.items():
            os.environ[k] = v
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["crash"]), 0)
        self.assertIn("push_nameproperty", buf.getvalue())
        server = Server()
        server.game = game
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "game", "arguments": {"action": "crash"}}})
        self.assertIn("push_nameproperty", reply["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
