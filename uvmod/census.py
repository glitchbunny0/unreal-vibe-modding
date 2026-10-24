"""The call census (lua/census.lua): which functions run when something happens. Hook every function of some
classes, count calls in windows, and compare an idle window with one in which the action happened."""

from __future__ import annotations

import re
import time

from . import helpers
from .game import Game
from .inputs import Input


def short(path: str) -> str:
    """/Game/X/BP_Door.BP_Door_C:Open -> BP_Door_C:Open"""
    owner, _, name = path.rpartition(":")
    return f"{re.split(r'[.]', owner)[-1]}:{name}" if owner else path


def render(lines: list[str]) -> str:
    out = []
    for line in lines:
        kind, _, rest = line.partition("\t")
        f = rest.split("\t")
        if kind == "H":
            out.append(f"counting calls of {f[0]} functions ({f[1]} could not be hooked, {f[2]} skipped)")
        elif kind == "C":
            out.append(f"window {f[0]}: {float(f[1]):.1f} s, {f[2]} calls of {f[3]} functions")
        elif kind == "F":
            out.append(f"  {int(f[0]):>7}  {float(f[1]):>8.1f}/s  {short(f[2])}")
        elif kind == "D":
            before, after = float(f[0]), float(f[1])
            note = "new" if before == 0 else f"x{after / before:.0f}"
            out.append(f"  {note:>5}  {before:>7.1f}/s -> {after:>7.1f}/s  {short(f[2])}")
        elif kind in ("V", "W"):
            out.append(rest)
    return "\n".join(out)


class Census:
    def __init__(self, game: Game, mod: str | None = None):
        self.game = game
        self.mod = mod

    def _call(self, expr: str, limit_ms: int = 3000) -> list[str]:
        return helpers.call(self.game, self.mod, helpers.CENSUS, expr, limit_ms)

    def start(self, refs: list[str], native: bool = False, pattern: str | None = None) -> str:
        if not refs:
            raise ValueError("name what to count: object references or class paths (pawn, pc, first:BP_Door_C, ...)")
        specs = "{" + ", ".join(helpers.lua_string(r) for r in refs) + "}"
        # inspect's resolve() knows pc / pawn / first: / cdo: / class:; make sure it is there.
        helpers.call(self.game, self.mod, helpers.INSPECT, "true", 3000)
        which = '"all"' if native else '"bp"'
        filt = helpers.lua_string(pattern) if pattern else "nil"
        return render(self._call(f"__uvmod_census.start({specs}, {which}, {filt})", 5000))

    def mark(self, limit: int = 40) -> str:
        return render(self._call(f"__uvmod_census.mark({int(limit)})"))

    def compare(self, factor: float = 3.0, limit: int = 60) -> str:
        return render(self._call(f"__uvmod_census.compare({float(factor)!r}, {int(limit)})"))

    def stop(self) -> str:
        return render(self._call("__uvmod_census.stop()"))

    def discover(self, refs: list[str], idle_s: float = 3.0, act_s: float = 5.0, press: str | None = None,
                 value: tuple[float, float, float] = (1.0, 0.0, 0.0), hold_ms: int = 0, native: bool = False,
                 pattern: str | None = None, prompt=None) -> str:
        """Start counting, take an idle window, then an action window (pressing `press` at its start, or telling the
        player through `prompt`), compare the two and remove the hooks again."""
        out = [self.start(refs, native, pattern)]
        try:
            self.mark()  # drop what ran while the hooks went in
            time.sleep(idle_s)
            out.append("idle " + self.mark(5).splitlines()[0])
            if press:
                out.append(Input(self.game, self.mod).press(press, value, hold_ms))
            elif prompt:
                prompt(f"act now ({act_s:g} s)")
            time.sleep(act_s)
            out.append("action " + self.mark(5).splitlines()[0])
            out.append(self.compare())
        finally:
            out.append(self.stop())
        return "\n".join(out)
