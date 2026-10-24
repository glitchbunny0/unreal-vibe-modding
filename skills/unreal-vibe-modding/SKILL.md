---
name: unreal-vibe-modding
description: Use when modding a running Unreal Engine game through UE4SS with uvmod (CLI or MCP tools). Probe, change and verify the game in small steps without crashing it.
---

# Vibe modding an Unreal Engine game with uvmod

The player runs the game; you reach it through `uvmod` (CLI) or the `unreal` MCP server's tools. Code you send runs as
Lua on the game thread, inside a UE4SS Lua mod, with the full power of a mod. The player's session is at stake: a
crash ends it, and so does a frozen game. Work in small steps, and verify each one before the next.

## Start of a session

1. `status`: is the game running, and which REPL answers? "UE4SS external REPL" can run code in any started mod (pick
   one with `--mod` or `UVMOD_MOD`; `mods` lists them). "VibeBridge" runs code in its own state only.
2. If nothing answers, tell the player what is missing: the game is not running, the external REPL is not turned on
   (`ExternalLuaReplEnabled = 1` under `[Debug]`, then restart), or VibeBridge is not installed (`uvmod
   install-bridge`, then restart).
3. Check the hot reload line. If it says unsafe and you will hook Blueprint functions, ask the player to set
   `EnableHotReloadSystem = 0`.
4. `inspect` `world` shows where the player is (map, controller, pawn); explore with `inspect` before writing Lua.
5. If `api` `info` says there is no index for this game build, build one (`uvmod index`, about 20 s). Look names and
   signatures up with `api` first: it covers everything the game had loaded and touches nothing.

## Rules that keep the game alive

These are native crashes, not Lua errors, and `pcall` does not catch them:

- **FName parameters.** A UFunction parameter of type `FName` needs `FName("Text")`. A Lua string or `nil` there
  crashes UE4SS. Call UFunctions with the `call` tool: it reads the signature and refuses these (and converts text
  to an `FName` where one is needed). In raw `lua`, check the function's signature before the first call.
- **Stale objects.** Check `obj:IsValid()` before using a UObject you got earlier: the game may have destroyed it.
  `FindFirstOf` returns an invalid object, not `nil`, when nothing matches.
- **Hot reload.** Once any mod hooks Blueprint functions, UE4SS's mod reload can hang or crash the game. Load new mod
  builds with a game restart, or build the mod as a hot-swap shell and use `swap`.

These freeze the game, or make it misbehave later:

- **Endless loops.** Requests stop after their limit (250 ms by default, `--limit` up to 5000). One long native call
  is not interrupted, and VibeBridge's limit is coarser, so keep loops bounded anyway.
- **Hooks are permanent until the mod restarts.** Probe with `watch`, which keeps track of its hooks and records
  into a bounded buffer; `unwatch` when done. A hook you register yourself with `RegisterHook` stays until the mod
  restarts: register it once and keep its ids in a global. A hook callback that errors on every call floods the log.
- **Post hooks on Blueprint functions never run.** UE4SS keeps only the pre callback of a Blueprint function's
  hook (`RegisterHook` then returns the same id twice); `watch --post` refuses them. Read the effect of a Blueprint
  call another way: a pre watch on whatever it calls next, or `wait --until` on the value. In a native post hook,
  the parameters after `self` start with the return value. Both checked on UE4SS 3.0.1, UE 5.6.
- **Captures run on every call.** A `watch` capture has no time limit and runs inside the game each time the
  function is called. Keep it to reading a few values; watching a per-frame function stops recording at its limit.

## How to find out what the game does

- **Read before you write.** `inspect` lists classes, properties with their current values, and functions with their
  signatures; `target` tells you what the camera is looking at. It is cheaper and safer than writing probe Lua.
- **Find the function before you watch it.** `calls` `discover` on the objects involved (pawn, pc, the thing the
  player handles) with `press` set to the action's InputAction lists what the action ran; then `watch` those with a
  capture.
- **A name is a guess.** A property called `IsHolding` can flip when an object is dropped, not when it is picked up.
  Measure it before you build on it: `snapshot` the object, tell the player what to do, then `diff`; or `watch` the
  function with a `capture` of the values you care about, then `wait` for it and read the events. `wait` with `until`
  does the same for a value.
- **Look at the game yourself.** Use `screenshot` instead of asking the player to describe the screen.
- **Play it yourself.** `input` presses or holds the game's own InputActions (IA_Jump, IA_Look with an X,Y value), so
  you can trigger the action you are studying and test a mod's controls without the player. Check that it did
  something (watch the `InpActEvt_<Action>_...` functions, snapshot, screenshot): an action only works where the
  game's mapping contexts are active. Say so before you move the character of a player who is playing.
- **Read the log after every change.** `log 50 'error|warning'` shows what your code caused.
- **Prefer the game's own functions over writing properties.** Calling the function keeps the game's own logic,
  replication and side effects. A direct property write can be local-only, or be overwritten next frame.

## When the game dies or freezes

- Run `game` `crash` before anything else, and read it: the error, the stack, and the last request sent from here
  (marked "no answer") usually name the cause. Do not send that request again in the same form.
- Then `game` `restart`, which waits until a REPL answers. Helpers (`watch`, `inspect`) and their state are gone after
  a restart; watches have to be set up again.
- A frozen game (no answer, process still running) is usually a long native call or an endless loop with the guard
  off. `game` `stop`, then restart.

## Building the mod

- Start a new mod with `uvmod new NAME` (a hot-swap shell, its logic, tests against a strict UE4SS stand-in) and
  load changes with `swap`, not UE4SS's hot reload. After the mod calls new UFunctions, `uvmod mock` refreshes the
  signatures its tests enforce.
- Run `api` `check` on the mod before every push: it finds the FName mistakes that would crash the game.
- Keep the probe code and the mod apart. Probe with `lua`; write the mod as files, `push` it, and restart the game to
  load it (or `swap` a hot-swap shell).
- Change one thing at a time, and after each change verify it with a measurement, a log line or a screenshot.
- When the player says something feels wrong, find the cause before adding a setting for it.
- Before you finish, list what you verified in the game and what you did not.

## What the answers look like

`lua` returns printed lines, then one `=> value` per returned value and `OK`, or `ERROR:` with a traceback. Values are
display strings: strings in quotes, `UObject: <full name>` or `(invalid)`, `FName: "..."`, tables as their entry
count. Globals persist between requests; locals do not.
