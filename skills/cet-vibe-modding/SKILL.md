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
3. `cet` `game_state` and `cet` `player_info` tell you where things stand. `Game.GetPlayer()` is `nil` in menus and
   on loading screens: check before using it.

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

## How to find out what the game does

- **Ready-made handlers first.** `cet` covers the player, inventory, equipment, status effects, nearby entities,
  what the player looks at (`get_scanner_info`), TweakDB, quest facts, and observing a game function's calls.
- **Then Lua.** The game's script API is what CET exposes: `Game.GetTransactionSystem()`, `Game.GetStatsSystem()`,
  `Game.GetScriptableSystemsContainer():Get("PreventionSystem")`, `GameTime.Hours(t)`, ... The decompiled game
  scripts (redscript) show how the game itself calls these, and NativeDB lists every type. Return
  `vb.json(value)` for structured answers, `vb.chunks(vb.json(value))` when it can be longer than 4 KB.
- **Look at the game yourself** with `screenshot`. It copies the game's window from the screen, so it only works while
  the game is the window in front; otherwise it refuses, and you ask the player to switch back.
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
