"""Playing the game through its own Enhanced Input (lua/input.lua): press or hold an InputAction, so an agent can
test a mod's controls without a person at the keyboard."""

from __future__ import annotations

from . import helpers
from .game import Game


def parse_value(text: str | None) -> tuple[float, float, float]:
    """"1" -> (1, 0, 0); "0.5,-1" -> (0.5, -1, 0); None -> (1, 0, 0) (a button press)."""
    if not text:
        return 1.0, 0.0, 0.0
    parts = [float(v) for v in text.replace(" ", "").split(",") if v != ""]
    if not 1 <= len(parts) <= 3:
        raise ValueError("a value is 1 to 3 numbers: X[,Y[,Z]]")
    parts += [0.0] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


class Input:
    def __init__(self, game: Game, mod: str | None = None):
        self.game = game
        self.mod = mod

    def _call(self, expr: str) -> str:
        lines = helpers.call(self.game, self.mod, helpers.INPUT, expr, 2000)
        return "\n".join(l.split("\t", 1)[1] if "\t" in l else l for l in lines if l)

    def list(self, pattern: str | None = None) -> str:
        rows = []
        for line in helpers.call(self.game, self.mod, helpers.INPUT,
                                 f"__uvmod_input.list({helpers.lua_string(pattern) if pattern else 'nil'})", 2000):
            kind, _, rest = line.partition("\t")
            if kind == "A":
                name, vtype, path = (rest.split("\t") + ["", ""])[:3]
                rows.append(f"{name}  ({vtype})  {path}")
            elif rest:
                rows.append(rest)
        return "\n".join(rows)

    def press(self, action: str, value: tuple[float, float, float] = (1.0, 0.0, 0.0), hold_ms: int = 0) -> str:
        x, y, z = value
        return self._call(f"__uvmod_input.press({helpers.lua_string(action)}, {x!r}, {y!r}, {z!r}, {int(hold_ms)})")

    def stop(self) -> str:
        return self._call("__uvmod_input.stop()")

    def status(self) -> str:
        return self._call("__uvmod_input.status()")


def press_key(game: Game, combo: str, hold_ms: int = 150) -> str:
    """An OS key press in the game's window, for UE4SS keybinds (see keys.py)."""
    from . import keys

    codes = keys.parse(combo)
    front = game.transport.press_keys(codes, max(50, min(int(hold_ms), 10000)))
    if front != "UnrealWindow":
        return (f"pressed {combo}, but the window in front was '{front or '?'}', not the game's: UE4SS keybinds only "
                "react while the game is in front")
    return f"pressed {combo} in the game's window for {hold_ms} ms"
