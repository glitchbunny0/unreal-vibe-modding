# The LuaRepl protocol

`unreal-vibe-modding` talks to a running Unreal Engine game through files in a folder. Two programs inside the game
can answer:

- **UE4SS itself** (the external Lua REPL, [UE4SS PR #1475](https://github.com/UE4SS-RE/RE-UE4SS/pull/1475); preview
  builds at [glitchbunny0/RE-UE4SS](https://github.com/glitchbunny0/RE-UE4SS/releases)). It can run code in any
  started Lua mod.
- **VibeBridge**, a Lua mod from this repository, for official UE4SS builds that have no external REPL. It can only
  run code in its own Lua state.

Both use the same folder, `<ue4ss folder>/LuaRepl`, and the same request and answer messages. They differ in how a
request is delivered, because a Lua mod cannot list a folder.

## Messages (protocol 1)

A request is one JSON object:

```json
{"protocol": 1, "op": "exec", "mod": "MyMod", "code": "return 1 + 1", "timeout_ms": 250}
```

| Field        | Meaning                                                                                              |
|--------------|------------------------------------------------------------------------------------------------------|
| `protocol`   | Always `1`.                                                                                          |
| `op`         | `exec` runs `code` in `mod`. `list` answers with the mods that `exec` can target.                   |
| `mod`        | `exec`: the name of a started Lua mod.                                                               |
| `code`       | `exec`: Lua source, tried as `return <code>` first and then as statements.                          |
| `timeout_ms` | Optional time limit, 1 to 5000 ms. Default 250.                                                      |
| `id`         | Only in requests to the slot (see below): the request's id.                                         |

An answer is one JSON object:

```json
{"protocol": 1, "id": "3f9a1c0e5b7d2468", "ok": true, "error": "", "values": ["2"], "prints": [], "mods": [],
 "truncated": false, "elapsed_ms": 0.02}
```

`values` are display strings. `prints` are the `print` calls made while the request ran. `error` holds the error
with a traceback, or the reason the request was refused. `mods` is filled for `list`. Unknown fields are ignored on
both sides.

An id is 8 to 32 lowercase hexadecimal characters, new for every request.

## Delivery

Every file is written to a temporary name in the folder and then renamed into place, so nobody reads a half-written
file. The answer always arrives as `<id>.response.json`; read it, then delete it.

**UE4SS (native).** Write the request as `<id>.request.json`. Any number of requests may wait; UE4SS runs one per
frame.

**VibeBridge.** While VibeBridge runs, it keeps `bridge.json` in the folder up to date, at least once a second:

```json
{"protocol": 1, "bridge": "VibeBridge", "version": "0.1.0", "mod": "VibeBridge", "time": 1798032000}
```

A client that finds a `bridge.json` written in the last 5 seconds writes the request, with its `id` inside, to the
single slot `bridge.request.json`, and waits for the answer before sending the next one. VibeBridge does not run when
the game's UE4SS has its own external REPL turned on (`[Debug] ExternalLuaReplEnabled = 1`), so the two never share
the folder.

## Safety

The folder is the trust boundary. Anything that can write to it can run Lua in the game with the full power of a
mod. There is no network listener. Do not use it in online play.
