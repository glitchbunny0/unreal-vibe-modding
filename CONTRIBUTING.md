# Contributing

Thanks for helping. Three kinds of contribution are especially welcome.

## Skills: what works when an agent mods a game

The [`skills/`](skills/) folder is meant to grow with experience. A good addition is a rule an agent can follow,
and the reason for it:

- **Say what you measured.** For example: "`FindFirstOf` returns an invalid object, not `nil`; checked on UE 5.6 and
  4.23." Name the UE version and the UE4SS build. A rule without evidence is a guess, and wrong guesses about a game
  are exactly what this project is trying to stop.
- **Write rules, not stories.** "Check `IsValid()` before using an object you got earlier", followed by why. Leave
  out the incident narrative.
- **Keep game-specific knowledge in a skill of its own**, for example `skills/<game>/SKILL.md`, and keep
  `skills/unreal-vibe-modding` general.
- **Use the [Agent Skills](https://agentskills.io) format:** one folder per skill, a `SKILL.md` with `name` and
  `description` in its front matter, and supporting files beside it.

## Code

- The client is Python 3.9+ with the standard library only; keep it that way.
- VibeBridge is plain Lua 5.4 with no dependencies. Logic goes in `vibebridge_core.lua`, which the tests run outside a
  game. UE4SS wiring goes in `main.lua`.
- Run the tests before a pull request: `python -m unittest discover -s tests -v` (the bridge tests need Lua 5.4; set
  `LUA=/path/to/lua`).
- Changes to [docs/protocol.md](docs/protocol.md) have to stay compatible with UE4SS's own external REPL, which speaks
  the same protocol.

## Reports from real games

Tell us which game, its UE version, which UE4SS build, and which REPL answered (`uvmod status`). Include the log lines
(`uvmod log 100`). If the game crashed, include UE4SS's crash dump if there is one.

## What does not belong here

- Game files, extracted assets, or anything else you have no right to share.
- Anything aimed at online play, other players' sessions, or getting around anti-cheat.
