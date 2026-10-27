--[[ VibeBridge for Cyber Engine Tweaks — answers the LuaRepl protocol (docs/protocol.md), so `uvmod` and its MCP
     server can drive Cyberpunk 2077. It runs code in its own environment (CET gives every mod its own).

     While it runs it keeps LuaRepl/bridge.json in its folder fresh (a heartbeat) and takes one request at a time from
     LuaRepl/bridge.request.json, runs it in onUpdate, and writes LuaRepl/<id>.response.json. CET lets a mod read and
     write only inside its own folder, so that is where the LuaRepl folder is.

     For requests it offers, besides everything CET gives a mod:
       vb.json(value [, depth])       JSON text for any value (tables, game objects, CET's types, 64-bit numbers)
       vb.chunks(text)                a long text as several return values, which uvmod joins again
       vb.query(name [, args])        run one of CETBridge's game handlers; JSON {"ok":..,"result"|"error":..}
       vb.handlers()                  their names
       cetbridge                      the handlers themselves (handlers.lua, from CETBridge: MIT, see LICENSE.CETBridge)

     DEVELOPMENT ONLY: anything that can write to the LuaRepl folder can run Lua in the game with the full power of a
     CET mod (which includes writing files and loading DLLs). There is no network listener. Never ship it to players. ]]

local VERSION = "0.1.0"
local MOD = "VibeBridge" -- CET names a mod after its folder; uvmod installs it as mods/VibeBridge
local POLL_SECONDS, BEAT_SECONDS = 0.025, 1.0
local FOLDER = "LuaRepl/"
local SLOT, INFO = FOLDER .. "bridge.request.json", FOLDER .. "bridge.json"

local real_print = print

local function log(fmt, ...)
    real_print(string.format("[VibeBridge] " .. fmt, ...))
end

-- CET's require returns nil and a message instead of raising; plain Lua's raises. Either way: module or nil, why.
local function load_module(name)
    local ok, m, err = pcall(require, name)
    if ok and m ~= nil then return m end
    return nil, tostring(ok and err or m)
end

local core, core_err = load_module("vibebridge_core")
if not core then
    log("v%s NOT running: vibebridge_core.lua did not load: %s", VERSION, core_err)
    return
end
local serializer = load_module("serializer")
local handlers, handlers_err = load_module("handlers")

local function read(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local text = f:read("*a")
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

local host = {
    mod = MOD,
    compile = function(code, name) return loadstring(code, name) end,
    set_print = function(f) print = f or real_print end,
}

local function userdata_json(v)
    if serializer then return serializer.serialize(v) end
end

local function json(value, depth)
    return core.json(value, userdata_json, depth)
end

local function handler_names()
    local names = {}
    for name, f in pairs(handlers or {}) do
        if type(f) == "function" and name:sub(1, 1) ~= "_" then names[#names + 1] = name end
    end
    table.sort(names)
    return names
end

local function query(name, args)
    local answer
    if not handlers then
        answer = { ok = false, error = "CETBridge's handlers did not load: " .. tostring(handlers_err) }
    elseif type(name) ~= "string" or name:sub(1, 1) == "_" or type(handlers[name]) ~= "function" then
        answer = { ok = false, error = "unknown handler '" .. tostring(name) .. "'; see vb.handlers()" }
    else
        local ok, result, err = pcall(handlers[name], args or {})
        if not ok then
            answer = { ok = false, error = tostring(result) }
        elseif err ~= nil then
            answer = { ok = false, error = tostring(err) }
        else
            answer = { ok = true, result = result }
        end
    end
    return core.chunks(json(answer))
end

vb = {
    version = VERSION,
    json = json,
    chunks = core.chunks,
    query = query,
    handlers = handler_names,
}
cetbridge = handlers

local function heartbeat()
    local extra = { handlers = tostring(#handler_names()) }
    local ok, cet = pcall(function() return GetVersion() end)
    if ok and type(cet) == "string" then extra.cet = cet end
    return write_atomic(INFO, core.encode_heartbeat("VibeBridge", VERSION, MOD, os.time(), extra))
end

local function serve(text)
    local ok, answer = pcall(core.handle, text, host)
    if not ok then
        host.set_print(nil)
        local request = core.json_decode(text)
        answer = { id = type(request) == "table" and request.id or "", ok = false, error = "VibeBridge internal error: " .. tostring(answer) }
    end
    if type(answer.id) ~= "string" or not answer.id:match("^[0-9a-f]+$") or #answer.id < 8 or #answer.id > 32 then
        log("dropped a request without a valid id")
        return
    end
    write_atomic(FOLDER .. answer.id .. ".response.json", core.encode_answer(answer))
end

local running, since_poll, since_beat = false, 0, 0

registerForEvent("onInit", function()
    if not heartbeat() then
        log("v%s NOT running: the folder %s does not exist in the mod (uvmod install-bridge creates it); reload mods after creating it",
            VERSION, FOLDER)
        return
    end
    running = true
    if handlers then
        log("v%s ready: %s (%d CETBridge handlers)", VERSION, FOLDER, #handler_names())
    else
        log("v%s ready: %s (CETBridge's handlers did not load: %s)", VERSION, FOLDER, tostring(handlers_err))
    end
end)

registerForEvent("onUpdate", function(delta)
    if not running then return end
    since_poll, since_beat = since_poll + delta, since_beat + delta
    if since_beat >= BEAT_SECONDS then
        since_beat = 0
        heartbeat()
    end
    if since_poll < POLL_SECONDS then return end
    since_poll = 0
    local text = read(SLOT)
    if not text then return end
    os.remove(SLOT)
    serve(text)
end)

return vb
