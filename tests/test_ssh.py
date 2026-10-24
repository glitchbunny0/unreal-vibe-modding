"""The ssh transport's session handshake, with a stand-in for ssh + PowerShell: a session that never reads its
input is retried, because nothing has run yet; other failures are reported."""

from __future__ import annotations

import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uvmod.transports.ssh as ssh  # noqa: E402
from uvmod.config import Settings  # noqa: E402
from uvmod.transports import TransportError  # noqa: E402

# Behaves like the bootstrap: reads one base64 line, says GO, "runs" the script by printing it back.
# Per attempt (counted in a file), MODES[n] picks: ok, stall (never reads), exit75, fail (dies before GO).
FAKE = r"""
import base64, sys, time
count_file, modes = sys.argv[1], sys.argv[2].split(",")
n = int(open(count_file).read() or 0) if __import__("os").path.exists(count_file) else 0
open(count_file, "w").write(str(n + 1))
mode = modes[min(n, len(modes) - 1)]
if mode == "stall":
    time.sleep(30)
if mode == "exit75":
    sys.exit(75)
if mode == "fail":
    sys.stderr.write("powershell: not recognized\n"); sys.exit(1)
line = sys.stdin.readline()
print("UVMOD-GO", flush=True)
script = base64.b64decode(line).decode()
if mode == "slow":
    time.sleep(30)
print("ran " + script.splitlines()[-1])
"""


class FakeSsh(ssh.SshTransport):
    def __init__(self, modes):
        super().__init__(Settings.load({"UVMOD_UE4SS": r"C:\x\ue4ss", "UVMOD_HOST": "box", "UVMOD_CONFIG": "/nonexistent"}))
        self.tmp = tempfile.TemporaryDirectory()
        self.count = Path(self.tmp.name) / "count"
        self.modes = modes

    def _command(self):
        return [sys.executable, "-c", FAKE, str(self.count), ",".join(self.modes)]

    def attempts(self):
        return int(self.count.read_text())


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        self.saved = ssh.GO_TIMEOUT
        ssh.GO_TIMEOUT = 0.5

    def tearDown(self):
        ssh.GO_TIMEOUT = self.saved

    def test_ok(self):
        t = FakeSsh(["ok"])
        self.assertEqual(t.powershell("'hello'", timeout=10), "ran 'hello'\n")
        self.assertEqual((t.attempts(), t.retries), (1, 0))

    def test_stalled_sessions_are_retried(self):
        t = FakeSsh(["stall", "exit75", "ok"])
        self.assertEqual(t.powershell("'again'", timeout=10), "ran 'again'\n")
        self.assertEqual((t.attempts(), t.retries), (3, 2))

    def test_gives_up_without_running(self):
        t = FakeSsh(["stall"])
        with self.assertRaises(TransportError) as e:
            t.powershell("'x'", timeout=10)
        self.assertIn("nothing was run", str(e.exception))
        self.assertEqual(t.attempts(), ssh.ATTEMPTS)

    def test_failure_before_go_is_reported_not_retried(self):
        t = FakeSsh(["fail", "ok"])
        with self.assertRaises(TransportError) as e:
            t.powershell("'x'", timeout=10)
        self.assertIn("not recognized", str(e.exception))
        self.assertEqual(t.attempts(), 1)

    def test_no_retry_once_started(self):
        t = FakeSsh(["slow", "ok"])
        with self.assertRaises(TransportError) as e:
            t.powershell("'x'", timeout=1)
        self.assertIn("started but did not finish", str(e.exception))
        self.assertEqual(t.attempts(), 1)

    def test_bootstrap_fits_the_command_line(self):
        self.assertLess(len(ssh._BOOT_ENCODED) + 60, 8191)
        self.assertIn("UVMOD-GO", base64.b64decode(ssh._BOOT_ENCODED).decode("utf-16-le"))


if __name__ == "__main__":
    unittest.main()
