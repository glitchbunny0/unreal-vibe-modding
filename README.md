# unreal-vibe-modding

Vibe modding for Unreal Engine games: let an AI coding agent (or you) run Lua live inside a running game through
[UE4SS](https://github.com/UE4SS-RE/RE-UE4SS), look at the result, and build a mod in small, checked steps. You play
and say what feels wrong; the agent probes the game, writes the mod, and verifies it.

- **`uvmod`**, a command-line tool: run Lua on the game thread, read and follow `UE4SS.log`, take screenshots, push
  mods, hot-swap mod logic without a restart.
- **`uvmod mcp`**, an [MCP](https://modelcontextprotocol.io) server with the same functions as tools, for agents such
  as Claude Code, Codex or Hermes.
- **VibeBridge**, a small Lua mod, so it also works with official UE4SS releases.
- **[Agent skills](skills/)**, for what works when an agent mods a game. Contributions welcome.

Python 3.9+, standard library only. MIT licensed.

> **Status: early.** The client and the bridge are tested end to end off-game (CI). Live so far, in one UE 5.6 game
> over SSH, with the CLI and the MCP server: VibeBridge on official UE4SS 3.0.1, and UE4SS's own external REPL on a
> preview build. Expect rough edges, and please report them.

## How it reaches the game

Two things inside the game can answer `uvmod`; it finds out which one on its own:

| | What you install | Where code runs |
|---|---|---|
| **UE4SS's own external Lua REPL** | A UE4SS build with it ([PR #1475](https://github.com/UE4SS-RE/RE-UE4SS/pull/1475); until it is merged, preview builds at [glitchbunny0/RE-UE4SS](https://github.com/glitchbunny0/RE-UE4SS/releases)), with `ExternalLuaReplEnabled = 1` under `[Debug]` | In any started Lua mod (`--mod`), on the game thread, with a time limit UE4SS enforces |
| **VibeBridge** | Any UE4SS build plus this repo's mod (`uvmod install-bridge`) | In VibeBridge's own Lua state, on the game thread, with a weaker time limit |

Both use files in `<game>/Binaries/Win64/ue4ss/LuaRepl/`; see [docs/protocol.md](docs/protocol.md). There is no network
listener. `uvmod` reaches that folder either directly (`local`: Windows, Linux with Proton, Steam Deck, WSL, a mounted
share) or over SSH to a Windows PC (`ssh`).

## Install

```sh
pip install git+https://github.com/glitchbunny0/unreal-vibe-modding
```

Then tell it where the game's ue4ss folder is, in `~/.config/uvmod/config` (or the same names as environment
variables):

```ini
UVMOD_UE4SS=C:\Program Files (x86)\Steam\steamapps\common\MyGame\MyGame\Binaries\Win64\ue4ss
# Only for a game on another Windows PC, reached over SSH with key login:
# UVMOD_HOST=player@192.168.1.20
# The mod whose Lua state runs code, if there is more than one (see: uvmod mods):
# UVMOD_MOD=MyMod
```

With an official UE4SS build, install the bridge once and restart the game:

```sh
uvmod install-bridge
```

With a UE4SS build that has the external REPL, set `ExternalLuaReplEnabled = 1` under `[Debug]` in
`UE4SS-settings.ini` instead. VibeBridge stays idle on such a build.

## Use

```sh
uvmod status                       # game running? which REPL answers? the settings that matter
uvmod lua '1 + 1'                  # => 2 / OK
uvmod lua 'return FindFirstOf("PlayerController"):GetFullName()'
uvmod run probe.lua                # a file, or - for stdin
uvmod log 50 'error|warning'       # UE4SS.log, filtered
uvmod shot                         # a screenshot of what the player sees
uvmod push MyMod                   # copy a mod folder into Mods/ (loads at the next game start)
uvmod swap MyMod                   # hot-swap a mod built as a hot-swap shell, no restart
```

`lua`, `run` and `mods` take `--mod NAME`, `--limit MS` and `--json`. Exit codes: 0 OK, 1 the Lua raised an error,
2 usage or settings, 3 nothing answered, 4 `wait` timed out.

### Reading the game, and calling into it safely

These read the game's reflection, so an agent can explore before it writes any Lua:

```sh
uvmod world                                   # world, player controller, pawn and where it is, game mode
uvmod find all:BP_Door_C                      # every live instance of a class
uvmod props pawn health                       # properties with types and current values (filtered by name)
uvmod funcs first:BP_Door_C open              # functions with signatures and flags
uvmod sig first:BP_Door_C OpenDoor            # one function's parameters
uvmod classes Door --base /Script/Engine.Actor  # loaded classes (or --kind struct|enum)
uvmod target                                  # what the camera is looking at (a line trace)
```

An object reference is a full path (`/Game/Maps/Level.Level:PersistentLevel.BP_Door_C_2`), `first:ShortClassName`,
`cdo:/Script/Pkg.Class` (the class default object), `class:/Script/Pkg.Class`, or `pc`, `pawn`, `world`.

`call` runs a UFunction behind a guard. It reads the parameters first and refuses what would crash UE4SS natively
rather than raise a Lua error: `nil` or a number for an `FName`, a non-table for a struct, an invalid object.
Arguments are JSON where they parse and text otherwise; text becomes an `FName`, an `FText` or the object a reference
names, wherever the parameter needs one:

```sh
uvmod call first:BP_Door_C OpenDoor pawn true
uvmod call pawn K2_SetActorLocation '{"X": 0, "Y": 0, "Z": 500}' false '{}' true
uvmod call pawn ActorHasTag --lua 'FName("Player")'
```

To find out what an action changes, snapshot an object, act, and diff:

```sh
uvmod snap before pawn          # keeps the readable property values inside the game
uvmod diff before               # what changed since; --update makes now the new baseline
```

### Playing it

So an agent can test controls itself, `input` plays the game through its own Enhanced Input: it injects a value for an
InputAction through the local player's `EnhancedInputLocalPlayerSubsystem`, once or on every frame for a while. The
game's mapping contexts, triggers and modifiers run as they do for a real key:

```sh
uvmod input list                              # loaded InputActions and their value types
uvmod input press IA_Jump                     # one frame
uvmod input press IA_Jump --hold 300          # held for 300 ms
uvmod input press IA_Look --value 1,0 --hold 500   # turn right for half a second
uvmod input stop
```

It uses `InjectInputVectorForAction`, because `InjectInputForAction` takes an `FInputActionValue`, a struct with no
reflected fields. Measured on RV There Yet? (UE 5.6, UE4SS b1a0bc22): `IA_Jump` fires the game's own input events,
and `IA_Look` held at 1,0 for 500 ms turns the camera by 120°. Games without Enhanced Input (most UE4 titles) are not
covered; there, `call` the input event's function directly.

### The game's reflection, offline

`uvmod index` asks the running game's UE4SS to dump its reflection (`DumpJMAP`, in UE4SS experimental build 1181 and
newer), copies the dump here and turns it into a SQLite index under `~/.local/share/uvmod/index/`. From then on,
questions about names and signatures need no game, cost nothing and cannot crash anything:

```sh
uvmod index                                   # once per game build; RV There Yet?: 46k objects, 20 s
uvmod api search plank                        # classes, structs, enums, functions, properties; ranked
uvmod api class BP_FirstPersonCharacter       # properties with their defaults, functions with signatures
uvmod api sig Actor:K2_SetActorLocation       # parameters with roles: in, out (pass {}), ref, return
uvmod api subclasses Pawn
uvmod check MyMod/                            # a mod's Lua against the index
```

`check` reports, by file and line, a string or `nil` passed where a UFunction takes an `FName` (an error: UE4SS
crashes natively on it, it is not a Lua error) and argument counts that no function of that name accepts. It exits 1
on errors, so it fits in a test script.

### Which functions run when something happens

Before you can `watch` a function you have to know its name. `calls discover` finds it: it hooks every Blueprint
function of the classes you name, counts calls in an idle window and in an action window, and lists what the action
ran. With `--press` it plays the action itself:

```sh
uvmod calls discover pawn pc --press IA_Jump --hold 300
#   new  0.0/s -> 6.8/s  BP_FirstPersonCharacter_C:InpActEvt_IA_Jump_K2Node_EnhancedInputActionEvent_18
#   new  0.0/s -> 0.4/s  BP_FirstPersonCharacter_C:K2_OnMovementModeChanged
#   new  0.0/s -> 0.2/s  BP_FirstPersonCharacter_C:CheckFallingDamage
uvmod calls discover first:BP_Door_C --act 10   # without --press it says when to act, and waits
```

`--native` adds native functions (never CoreUObject's), `--pattern` limits the hooks by name. The hooks are removed at
the end; `calls start`, `mark`, `compare` and `stop` do the same one step at a time. On RV There Yet? the character
and controller have 582 Blueprint functions; hooking, counting and removing them all took 14 s.

### Watching what the game does

To learn what happens when the player does something, watch the function, let the player act, then read what was
recorded. The recording happens inside the game, in a bounded buffer, so nothing is lost between calls and the log
stays clean:

```sh
uvmod watch /Game/Blueprints/BP_Door.BP_Door_C:OpenDoor --capture 'self.bIsOpen, select(1, ...)'
uvmod wait --for OpenDoor          # blocks until it is called (default 30 s); exit 4 if not
uvmod events                       # everything recorded since the last events/wait, runs collapsed
uvmod wait --until 'FindFirstOf("BP_Door_C").bIsOpen' --timeout 60
uvmod unwatch                      # remove every watch
```

`--capture` is a Lua expression over `self` and the function's parameters (`...`, already read); its value is
recorded, and `nil` skips the call. A watch records at most `--limit` calls (default 200) and keeps counting after
that. The capture runs inside the game on every call, without a time limit, so keep it short.

`--post` records after the call instead. UE4SS runs post callbacks for native functions only, so `watch` refuses
`--post` on a Blueprint function. In a post capture, `...` starts with the function's return value, if it has one.

### Starting the game, and when it dies

```sh
uvmod restart                   # stop, launch, wait until a REPL answers: how new mod builds load
uvmod launch | uvmod stop       # Steam (app id read from the Steam library, or UVMOD_APPID), or UVMOD_LAUNCH
uvmod ready                     # wait until a REPL answers (default 180 s)
uvmod crash                     # why it died
uvmod journal                   # the last requests sent from here, and how each ended
```

`crash` puts together what says why the game died: the newest Unreal crash folder (its error message and call stack,
and the exception, faulting module and return addresses read from its minidump), UE4SS's own crash dump, the end of
`UE4SS.log`, and the last requests uvmod sent. uvmod writes every request to a journal before it goes out
(`~/.local/state/uvmod/journal.jsonl`; `UVMOD_JOURNAL=off` turns it off), so a request without an answer right before
a crash is the first suspect. Module offsets symbolize with the PDB of the same build (`llvm-symbolizer`).

With the ssh transport, `launch` starts the game through a scheduled task (`uvmod-launch`) in the logged-in user's
session, because a program started over SSH has no desktop.

### As an MCP server

```json
{ "mcpServers": { "unreal": { "command": "uvmod", "args": ["mcp"] } } }
```

Tools: `lua`, `mods`, `status`, `log`, `screenshot`, `inspect`, `call`, `snapshot`, `api`, `input`, `calls`, `watch`,
`events`, `wait`, `unwatch`, `push`, `swap`, `game`. Their descriptions carry the rules that crash games when broken, because that is what an agent reads.

## Safety

- Code you send runs with the full power of a mod. Some mistakes are not Lua errors but native crashes: a Lua string
  or `nil` where a UFunction expects an `FName`, or a UObject used after the game destroyed it. The skill and the tool
  descriptions spell these out.
- Both REPLs stop a runaway Lua loop after a time limit (250 ms by default, at most 5000). That is a guard against
  mistakes, not a sandbox: one long native call is not interrupted, and code can turn the guard off. VibeBridge's
  limit is coarser than UE4SS's (an instruction budget; plain Lua in a game has no fine clock).
- Anything that can write to the `LuaRepl` folder can run code in the game. Keep it for single-player games and your
  own sessions. Never use it in online play or against anti-cheat-protected games.
- VibeBridge is a development tool. Do not ship it to players.

## Agent skills

[`skills/`](skills/) holds skills in the [Agent Skills](https://agentskills.io) format, one folder per skill with a
`SKILL.md`. Point your agent at them, or copy them into its skills folder. They start with the basics and are meant to
grow with what people learn while vibe modding; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Tests

```sh
python -m unittest discover -s tests -v    # the bridge tests need Lua 5.4 (set LUA=/path/to/lua)
```

`tests/fake_game.py` answers the protocol the way UE4SS and VibeBridge do; `tests/fake_ue4ss.lua` runs the real
VibeBridge mod outside a game.

## History

This replaces [ue4ss-devbridge](https://github.com/glitchbunny0/ue4ss-devbridge), its bash predecessor with its own
protocol. It came out of a few days of a human and an AI agent modding *RV There Yet?* together.

## License

MIT. See [LICENSE](LICENSE).
