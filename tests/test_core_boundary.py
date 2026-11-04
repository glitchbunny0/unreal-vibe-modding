"""The core boundary: uvmod.core imports nothing from uvmod outside itself, cetmod builds only on uvmod.core, and a
third app can sit on the same core with nothing of its own changed (a fake one here, against the fake game), with
CORE_API as the versioned contract between them."""

from __future__ import annotations

import ast
import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cetmod  # noqa: E402
from fake_game import FakeGame  # noqa: E402
from uvmod.core import CORE_API  # noqa: E402
from uvmod.core.app import App  # noqa: E402
from uvmod.core.cli import status_text  # noqa: E402
from uvmod.core.config import ConfigError, Settings  # noqa: E402
from uvmod.core.engine import Engine  # noqa: E402
from uvmod.core.session import Session  # noqa: E402


def package_of(path: Path) -> str:
    """The dotted package a .py file under the repo root lives in ('' for a top-level module)."""
    return ".".join(path.relative_to(ROOT).parts[:-1])


def resolved_imports(path: Path) -> set[str]:
    """The absolute module names every import in `path` resolves to (relative imports walked from its own package)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: `level` dots go up from this module's package
                parts = package_of(path).split(".")
                base = parts[: len(parts) - (node.level - 1)] if package_of(path) else []
            else:
                base = []
            if node.module:
                base = base + node.module.split(".")
            out.add(".".join(base))
    return out


def py_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.py") if "__pycache__" not in p.parts)


class Imports(unittest.TestCase):
    def test_core_imports_only_core(self):
        for path in py_files(ROOT / "uvmod" / "core"):
            for name in resolved_imports(path):
                if name.startswith("uvmod"):
                    self.assertTrue(name == "uvmod.core" or name.startswith("uvmod.core."),
                                    f"{path.relative_to(ROOT)} imports {name}")

    def test_cetmod_builds_only_on_core(self):
        for path in py_files(ROOT / "cetmod"):
            for name in resolved_imports(path):
                if name.startswith("uvmod"):
                    self.assertTrue(name == "uvmod.core" or name.startswith("uvmod.core."),
                                    f"{path.relative_to(ROOT)} imports {name}")


# A third app on the same core, defined entirely here: an engine with the fake game's layout and a session that needs
# nothing of its own for status, exec and log.
FAKE_ENGINE = Engine(
    name="fakegame",
    label="FakeGame",
    repl="LuaRepl",
    mods="Mods",
    log="FakeGame.log",
    bridge_marker="Mods/VibeBridge/enabled.txt",
    reporter="CrashReportClient",
    status_pattern="Fake",
)
FAKE_APP = App(name="fakemod", prefix="FAKEMOD", root_key="GAME", root_help="the fake game's folder",
               engine=FAKE_ENGINE, keys={})


class FakeAppSettings(Settings):
    @classmethod
    def load(cls, overrides: dict[str, str] | None = None) -> "FakeAppSettings":
        return super().load(FAKE_APP, overrides)


class ThirdApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.game_root = root / "fakegame"
        self.game_root.mkdir()
        (self.game_root / "FakeGame.log").write_text("[x] Fake - v1\n[x] started\n")
        self.fake = FakeGame(self.game_root / "LuaRepl", "native")
        self.fake.start()
        self.env = {"FAKEMOD_GAME": str(self.game_root), "FAKEMOD_TRANSPORT": "local", "FAKEMOD_TIMEOUT": "2",
                    "FAKEMOD_CONFIG": "/nonexistent", "FAKEMOD_JOURNAL": str(root / "journal.jsonl")}

    def tearDown(self):
        self.fake.stop()
        self.tmp.cleanup()

    def test_settings_error_names_the_app(self):
        with self.assertRaises(ConfigError) as ctx:
            FakeAppSettings.load({"FAKEMOD_CONFIG": "/nonexistent"})
        self.assertTrue(str(ctx.exception).startswith("FAKEMOD_GAME is not set"), ctx.exception)

    def test_status_exec_and_log(self):
        session = Session(FakeAppSettings.load(self.env))
        result = session.exec("1 + 1", "Alpha")
        self.assertTrue(result.ok, result.answer)
        self.assertEqual(result.answer["values"], ["2"])
        self.assertEqual(session.log(5), ["[x] Fake - v1", "[x] started"])
        body, rc = status_text(session)
        self.assertIn("game: unknown (set FAKEMOD_PROCESS)", body)


class CoreApi(unittest.TestCase):
    def test_mismatch_raises_with_both_numbers(self):
        with mock.patch.object(importlib.import_module("uvmod.core"), "CORE_API", 2):
            with self.assertRaises(ImportError) as ctx:
                importlib.reload(cetmod)
        importlib.reload(cetmod)  # leave the real one imported
        message = str(ctx.exception)
        self.assertIn("CORE_API 1", message)  # what cetmod needs
        self.assertIn("found 2", message)  # what the test pretended core to be
        self.assertEqual(CORE_API, 1)


if __name__ == "__main__":
    unittest.main()
