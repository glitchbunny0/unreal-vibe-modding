--[[ VibeBridge — answers the LuaRepl protocol (docs/protocol.md) for UE4SS builds that have no external Lua REPL,
     so `uvmod` and its MCP server work with official UE4SS releases too. It runs code in its own Lua state only.

     While it runs it keeps <ue4ss>/LuaRepl/bridge.json fresh (a heartbeat) and takes one request at a time from
     <ue4ss>/LuaRepl/bridge.request.json, runs it on the game thread, and writes <id>.response.json.

     It does nothing when this UE4SS has its own external REPL turned on ([Debug] ExternalLuaReplEnabled = 1).

     DEVELOPMENT ONLY: anything that can write to the LuaRepl folder can run Lua in the game. Never ship it to
     players or use it in online play. ]]

local VERSION = "0.1.0"
local POLL_MS = 25

local core = require("vibebridge_core")

local function log(fmt, ...)
    print(string.format("[VibeBridge] " .. fmt .. "\n", ...))
end

-- Paths from this script's own location: <ue4ss>/Mods/<name>/Scripts/main.lua. If the mod lives somewhere else
-- (UE4SS_MODS_PATHS), fall back to paths relative to the working directory, which UE4SS sets to the ue4ss folder.
local source = debug.getinfo(1, "S").source or ""
local mod_dir, sep = source:match("^@(.*)([/\\])Scripts[/\\]main%.lua$")
sep = sep or "\\"
local MOD = mod_dir and mod_dir:match("([^/\\]+)$") or "VibeBridge"
local UE4SS = mod_dir and mod_dir:match("^(.*)[/\\][Mm]ods[/\\][^/\\]+$")
local function in_ue4ss(name) return UE4SS and (UE4SS .. sep .. name) or name end
local FOLDER = in_ue4ss("LuaRepl") .. sep
local SLOT, INFO = FOLDER .. "bridge.request.json", FOLDER .. "bridge.json"

local function read(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local text = f:read("a")
    f:close()
    return text
end

local function write_atomic(path, text)
    local tmp = path .. ".tmp"
    local f = io.open(tmp, "wb")
    if not f then return false end
    f:write(text)
    f:close()
    os.remove(path)
    return os.rename(tmp, path)
end

local function native_repl_enabled()
    local ini = read(in_ue4ss("UE4SS-settings.ini")) or ""
    local enabled = false
    for line in ini:gmatch("[^\r\n]+") do
        local v = line:match("^%s*ExternalLuaReplEnabled%s*=%s*(%d+)")
        if v then enabled = v ~= "0" end
    end
    return enabled
end

if native_repl_enabled() then
    log("v%s idle: this UE4SS has its own external Lua REPL turned on, which answers in %s", VERSION, FOLDER)
    return
end
if not write_atomic(INFO, core.encode_heartbeat("VibeBridge", VERSION, MOD, os.time())) then
    log("v%s NOT running: the folder %s does not exist (uvmod install-bridge creates it); restart the game after creating it",
        VERSION, FOLDER)
    return
end

local env, set_capture = core.new_env(_G, nil)
local last_beat = os.time()

local function serve(text)
    local ok, answer = pcall(core.handle, text, env, set_capture, MOD)
    if not ok then
        set_capture(nil)
        answer = { id = (core.json_decode(text) or {}).id, ok = false, error = "VibeBridge internal error: " .. tostring(answer) }
    end
    if type(answer.id) ~= "string" or not answer.id:match("^[0-9a-f]+$") or #answer.id < 8 or #answer.id > 32 then
        log("dropped a request without a valid id")
        return
    end
    if not write_atomic(FOLDER .. answer.id .. ".response.json", core.encode_answer(answer)) then
        log("could not write the answer to %s", answer.id)
    end
end

-- The async thread watches the slot and keeps the heartbeat fresh; requests run on the game thread.
LoopAsync(POLL_MS, function()
    local now = os.time()
    if now ~= last_beat then
        last_beat = now
        write_atomic(INFO, core.encode_heartbeat("VibeBridge", VERSION, MOD, now))
    end
    local text = read(SLOT)
    if text then
        os.remove(SLOT)
        ExecuteInGameThread(function() serve(text) end)
    end
    return false
end)

log("v%s running: answers requests in %s for mod '%s'. DEVELOPMENT ONLY: anything that can write there can run Lua here.",
    VERSION, FOLDER, MOD)
