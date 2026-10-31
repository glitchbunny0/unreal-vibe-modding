# Contributing

Thanks for helping. Three kinds of contribution are especially welcome: what an agent learned about a game (skills),
new capabilities (code), and reports from real games.

Where things go, for Cyberpunk 2077: tool code, the CET bridge, its game handlers and the skill on driving the game
(`skills/cet-vibe-modding`) live here; the game's measured API facts, source pins, reviews and build scripts live in
the [cp2077-vibe-modding](https://github.com/glitchbunny0/cp2077-vibe-modding) kit (its `CONTRIBUTING.md`).

## Skills: what works when an agent mods a game

The [`skills/`](skills/) folder is meant to grow with experience. A good addition is a rule an agent can follow,
and the reason for it:

- **Say what you measured, and on what.** For example: "`FindFirstOf` returns an invalid object, not `nil`; checked on
  UE 5.6 and 4.23." Name the UE version and the UE4SS build, or for Cyberpunk 2077 the game patch and the CET version.
  A rule without evidence is a guess, and wrong guesses about a game are exactly what this project is trying to stop.
- **Write rules, not stories.** "Check `IsValid()` before using an object you got earlier", followed by why. Leave
  out the incident narrative.
- **Keep game-specific knowledge in a skill of its own**, for example `skills/<game>/SKILL.md`, and keep
  `skills/unreal-vibe-modding` and `skills/cet-vibe-modding` about driving the engine with uvmod.
- **Use the [Agent Skills](https://agentskills.io) format:** one folder per skill, a `SKILL.md` with `name` and
  `description` in its front matter, and supporting files beside it.

## Code

- The client is Python 3.9+ with the standard library only; keep it that way.
- VibeBridge for UE4SS is plain Lua 5.4 with no dependencies. Logic goes in `vibebridge_core.lua`, which the tests run
  outside a game. UE4SS wiring goes in `main.lua`.
- VibeBridge for CET (`uvmod/bridge/cet/VibeBridge/`) runs under CET's LuaJIT. `tests/fake_cet.lua` stands in for CET's
  sandbox; the tests run it under Lua 5.4 and, with `pip install lupa`, under LuaJIT too. Install lupa before a pull
  request that touches it: CI runs both.
- **A new CET game handler** (a ready-made call behind `uvmod cet NAME` and the MCP tool `cet`):
  1. Write it in `cetbridge_fixes.lua` as `handlers.<name> = function(args) ... end`, beside `overview`. Leave
     `handlers.lua` unmodified: it is CETBridge's upstream file. Wrap game calls in `try`/`pcall` so a missing field
     returns `nil` instead of an error.
  2. Describe it in `uvmod/cet.py`, under `READS` if it changes nothing in the game, under `CHANGES` if it does (the
     MCP tool tells the agent to ask the player before a change). `test_catalog_matches_the_lua_handlers` fails until
     both sides agree.
  3. Give `tests/fake_cet.lua` what the handler calls and add a case to `tests/test_cet_e2e.py`.
  4. Say in the pull request on which patch and CET version you ran it in the game, and what it returned. A handler
     that changes the game says what it changes in the player's save.
- Run the tests before a pull request: `python -m unittest discover -s tests -v` (the bridge tests need Lua 5.4; set
  `LUA=/path/to/lua`).
- Changes to [docs/protocol.md](docs/protocol.md) have to stay compatible with UE4SS's own external REPL, which speaks
  the same protocol.

## Reports from real games

Tell us which game and which mod loader: for Unreal games the UE version, the UE4SS build and which REPL answered; for
Cyberpunk 2077 the game patch, the CET version and the store (Steam, GOG, Epic). Include `uvmod status` and the log
lines (`uvmod log 100`). If the game crashed or froze, include `uvmod crash` and, for Unreal games, UE4SS's crash dump if
there is one.

## What does not belong here

- Game files, extracted assets, or anything else you have no right to share.
- Anything aimed at online play, other players' sessions, or getting around anti-cheat.

Contributions are accepted under this repository's MIT license.
