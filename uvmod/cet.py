"""CETBridge's game handlers (bridge/cet/VibeBridge/handlers.lua, MIT, from cyber-engine-tweak-mcp via WkMCP), as
VibeBridge offers them through `vb.query`. The catalog here describes them for the CLI and the MCP tool; a test
checks it against the handlers in the Lua file."""

from __future__ import annotations

import json
import re
from importlib import resources

# name: (arguments, what it does). READS change nothing in the game; CHANGES do.
READS: dict[str, tuple[str, str]] = {
    "player_info": ("", "level, street cred, health, max health, position"),
    "game_state": ("", "game time, scene tier (gameplay, cinematic, ...), weather, security zone"),
    "get_equipped": ("", "items in the player's equipment slots"),
    "get_inventory": ("type?, limit?", "inventory items (TweakDB ids, names, quantities); type: part of the record class name"),
    "get_active_effects": ("", "status effects on the player"),
    "get_appearance_info": ("", "the player's appearance"),
    "get_vehicle_list": ("", "vehicles the player owns"),
    "get_nearby_entities": ("radius?, type?, limit?", "entities around the player (type: NPC, Vehicle, Item, Device) with class, distance, position"),
    "get_scanner_info": ("", "the entity the player looks at: class, name, level, health, position"),
    "get_quest_fact": ("factName", "a quest fact's value (story flags, e.g. q001_rogue_met)"),
    "tweakdb_get": ("path", "a TweakDB flat or record, e.g. Items.Preset_Katana_Saburo"),
    "search_tweakdb": ("pattern, type?, limit?", "TweakDB record ids matching a pattern"),
    "dump_type": ("typeName", "a game RTTI type's properties and functions, e.g. PlayerPuppet"),
    "observe_events": ("className, eventName, maxBuffer?", "start recording calls of a game function (ObserveAfter); returns a subscriptionId"),
    "get_observations": ("subscriptionId", "the calls recorded since the last read (and clears them)"),
    "unobserve_events": ("subscriptionId", "stop recording (CET keeps the hook, which then does nothing)"),
    "probe": ("", "a health check of the game's script runtime and of known CET mods"),
}
CHANGES: dict[str, tuple[str, str]] = {
    "tweakdb_set": ("path, type, value", "write a TweakDB flat (until the game restarts; wrong values can crash it)"),
    "add_item": ("itemId, quantity?", "give the player an item by TweakDB id"),
    "remove_item": ("itemId, quantity?", "take an item from the player"),
    "teleport": ("x, y, z", "move the player"),
    "set_stat": ("stat, value", "set a player stat with a modifier (a repeat call replaces it)"),
    "apply_status_effect": ("effectId", "apply a status effect, e.g. BaseStatusEffect.Berserk"),
    "remove_status_effect": ("effectId", "remove a status effect"),
    "spawn_vehicle": ("vehicleId", "summon a vehicle; the game places it"),
    "set_time": ("hours, minutes?, seconds?", "set the game time"),
    "set_weather": ("weather", "set the weather: Sunny, Cloudy, Rain, HeavyRain, Fog, Toxic, Sandstorm, Pollution"),
    "set_quest_fact": ("factName, value", "set a quest fact (can break quest progression)"),
    "toggle_god_mode": ("enabled", "make the player invulnerable or not"),
    "set_level": ("level?, streetCred?", "set the player's level and street cred"),
    "kill_nearby_npcs": ("radius?, allNpcs?", "kill hostile NPCs around the player (all NPCs with allNpcs)"),
    "show_notification": ("message", "show a message on the HUD"),
    "play_sound": ("soundEvent", "play a sound event"),
    "batch_execute": ("commands", "run several Lua snippets (prefer the lua tool)"),
}
HANDLERS = {**READS, **CHANGES}


def lua_handlers() -> list[str]:
    """The handler names defined in the bundled handlers.lua."""
    source = (resources.files("uvmod") / "bridge" / "cet" / "VibeBridge" / "handlers.lua").read_text(encoding="utf-8")
    return sorted(set(re.findall(r"^function handlers\.([a-z_]+)\(", source, re.M)))


def catalog() -> str:
    lines = ["reads (change nothing):"]
    lines += [f"  {name}({args})  {what}" for name, (args, what) in READS.items()]
    lines.append("changes the game:")
    lines += [f"  {name}({args})  {what}" for name, (args, what) in CHANGES.items()]
    return "\n".join(lines)


def parse_args(text: str | None) -> dict:
    """Arguments from the command line: a JSON object, or key=value pairs (values parsed as JSON where they parse)."""
    if not text:
        return {}
    text = text.strip()
    if text.startswith("{"):
        value = json.loads(text)
        if not isinstance(value, dict):
            raise ValueError("handler arguments are a JSON object")
        return value
    out: dict = {}
    for pair in text.split():
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"expected key=value, got {pair!r}")
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw
    return out


def render(answer: dict) -> str:
    if not answer.get("ok"):
        return f"ERROR: {answer.get('error', '')}"
    return json.dumps(answer.get("result"), indent=2, ensure_ascii=False)
