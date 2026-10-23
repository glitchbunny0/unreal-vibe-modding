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
2 usage or settings, 3 nothing answered.

### As an MCP server

```json
{ "mcpServers": { "unreal": { "command": "uvmod", "args": ["mcp"] } } }
```

Tools: `lua`, `mods`, `status`, `log`, `screenshot`, `push`, `swap`. Their descriptions carry the rules that crash games
when broken, because that is what an agent reads.

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
