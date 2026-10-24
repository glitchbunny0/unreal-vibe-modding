# {{NAME}}

A UE4SS Lua mod, made with `uvmod new`.

## Layout

- `{{NAME}}/` is the mod folder, as it goes into the game's `ue4ss/Mods/`.
  - `Scripts/main.lua` is the hot-swap shell: it registers the keys and hooks once and forwards them into the logic.
    Change it rarely; a change needs a game restart.
  - `Scripts/logic.lua` is the mod. Edit it and swap it into the running game without a restart.
- `tests/` runs without the game: `tests/ue4ss.lua` stands in for UE4SS and crashes like the real game on an FName
  mistake, `tests/game_api.lua` holds the real signatures of the functions the mod calls (`uvmod mock` writes it).

## Loop

```sh
tests/run.sh                 # syntax, uvmod check, tests
uvmod push {{NAME}}          # first time: copy the mod into the game, then
uvmod restart                # start the game with it
uvmod swap {{NAME}}          # after that: load a changed logic.lua into the running game
uvmod log 30 {{NAME}}        # what the mod said
```

## Test plan (in the game)

What to check by hand, and what the log should say:

1. Start the game: the log says `[{{NAME}}] v0.1.0 loaded (hot-swap shell 1)`.
2. In a level, press F8: the log says where the player is.

Status: untested in the game.
