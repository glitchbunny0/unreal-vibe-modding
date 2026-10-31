---
name: cet-vibe-modding
description: Use when modding a running Cyberpunk 2077 through Cyber Engine Tweaks with uvmod (CLI or MCP tools). Probe, change and verify the game in small steps without freezing it.
---

# Vibe modding Cyberpunk 2077 with uvmod and Cyber Engine Tweaks

The player runs the game with Cyber Engine Tweaks (CET) and the VibeBridge CET mod; you reach it through `uvmod`
(with `UVMOD_CET` set) or the MCP server's tools. Code you send runs as Lua (LuaJIT) inside VibeBridge's environment,
in CET's `onUpdate`, with the full power of a CET mod. A crash ends the player's session, and a frozen game too.

## Start of a session

1. `status`: is the game running, and does VibeBridge answer? It shows VibeBridge's and CET's versions.
2. If nothing answers: the game is not running, VibeBridge is not installed (`uvmod install-bridge`), or CET has not
   loaded it yet. CET loads mods at game start and when the player clicks "Reload all mods" in CET's overlay; ask
   the player to do that, it takes a second and keeps their session.
3. `cet` `overview` tells you where things stand: district, eddies, time, tracked quest, vehicle. `Game.GetPlayer()`
   is `nil` in menus and on loading screens: check before using it.

## Rules that keep the game alive

- **No time limit.** CET gives mods no debug library, so VibeBridge cannot stop a request (`limit_ms` is checked,
  not enforced). A loop without a bound freezes the game for good. Bound every loop.
- **Hooks stay until CET reloads its mods.** `ObserveAfter`, `ObserveBefore` and `Override` cannot be removed by a
  mod. Register each one once (keep a flag in a global) and keep callbacks short: they run on every call. CETBridge's
  `observe_events` buffers calls for you and `unobserve_events` turns its callback into a no-op.
- **Ask before changing the save.** Items, quest facts, levels and stats end up in the player's next save. The `cet`
  tool's list marks the handlers that change the game.
- **64-bit numbers are LuaJIT cdata** (entity ids, some quantities): `tonumber()` them before arithmetic. `vb.json`
  and the value display handle them.
- **`EnumInt` only on a real enum value.** Given a plain Lua number it kills the game outright (a native crash, not a
  Lua error), and a getter that reads like an enum may return a number (`PlayerPuppet.GetSceneTier` does on 2.5).
  Check `type(x) == "userdata"` first when a value could be either.
- **A step that schedules itself must land in a later frame.** If code in `onUpdate` walks a list of pending steps and
  a step adds another one already due (time <= now), the walk reaches it in the same frame, the frame never ends, and
  the engine's watchdog kills the game after 120 s. Schedule at least a millisecond ahead.

The game's measured 2.5 API (calling conventions, HUD and ink, effects, devices, on-screen text and phone calls,
screenshots under HDR, custom meshes) is the `cp2077-vibe-modding` skill in the
[cp2077-vibe-modding](https://github.com/glitchbunny0/cp2077-vibe-modding) kit. Load it before writing anything beyond
a quick probe.

## How to find out what the game does

- **Ready-made handlers first.** `cet` covers the player, inventory, equipment, status effects, nearby entities,
  what the player looks at (`get_scanner_info`), TweakDB (search by name, records with their fields), types
  (`dump_type` lists a class's functions with signatures), quest facts, and observing a game function's calls. The
  weather cannot be set on patch 2.5 (`set_weather` says so). When a probe you wrote would be worth having as a handler
  for everyone, offer the player to contribute it (`CONTRIBUTING.md`, "A new CET game handler").
- **CET's calling conventions.** A game function's out parameters come back as extra return values, success first:
  `local ok, items = ts:GetItemList(player)`. A record's `DisplayName()` is a LocKey CName: read it with
  `Game.GetLocalizedTextByKey(record:DisplayName())`.
- **Then Lua.** The game's script API is what CET exposes: `Game.GetTransactionSystem()`, `Game.GetStatsSystem()`,
  `Game.GetScriptableSystemsContainer():Get("PreventionSystem")`, `GameTime.Hours(t)`, ... The decompiled game
  scripts (redscript) show how the game itself calls these, and NativeDB lists every type. Return
  `vb.json(value)` for structured answers, `vb.chunks(vb.json(value))` when it can be longer than 4 KB.
- **Look at the game yourself** with `screenshot`. It copies the game's window from the screen, so it only works while
  the game is the window in front; otherwise it refuses, and you ask the player to switch back. When you run on the
  game PC, your own window is usually the one in front: ask the player to switch to the game, then take the shot.
- **CLI from Windows PowerShell 5.1:** write strings inside the Lua with single quotes; it drops embedded double quotes
  from arguments. Or use `uvmod run file.lua`, or the MCP tools.
- **The log lags.** CET writes `scripting.log` out when a warning flushes it, so recent info lines may be missing.
  Your prints come back in the answer anyway.

## When the game dies or freezes

- `game` `crash` first: REDEngine's crash report (stack trace, report, the minidump's exception), the end of
  `scripting.log`, and the last request sent from here. Do not resend the request that got no answer.
- A frozen game with the process still running is usually a request that never ends. `game` `stop`, then `restart`.

## What the answers look like

`lua` returns printed lines, then one `=> value` per returned value and `OK`, or `ERROR:` without a traceback. Game
objects show as `object: ClassName`, CET's types (Vector4, CName, TweakDBID, ItemID) as their constructor text, tables
as their entry count. Globals persist between requests; locals do not. `cet` returns the handler's result as JSON.
