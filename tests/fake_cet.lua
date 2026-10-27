--[[ Runs the real CET VibeBridge (bridge/cet/VibeBridge/init.lua) outside the game, in a stand-in for Cyber Engine
     Tweaks' mod sandbox: only the globals CET gives a mod (no debug, no load, no utf8, no setfenv), loadstring and
     require that put code into the mod's environment, io and os confined to the mod's folder, print into
     <cet>/scripting.log, registerForEvent with onInit and onUpdate. Stops after SECONDS or when <cet>/STOP appears.

     lua5.4 tests/fake_cet.lua <cet folder> [SECONDS]            (Lua 5.4)
     python tests/luajit_run.py tests/fake_cet.lua <cet> [S]     (LuaJIT, CET's own Lua, through lupa)

     A small fake of the game's API lets CETBridge's handlers run: the player, stats, inventory, targeting, TweakDB
     records, DumpType and game time, in the shapes CET 1.38 gives on patch 2.5. Under LuaJIT, game objects are
     userdata (newproxy) and the player's entity id is a 64-bit cdata number, as in CET. ]]

local CET, seconds = arg[1], tonumber(arg[2] or "30")
local MOD = CET .. "/mods/VibeBridge"
local host = _G

local logf = assert(io.open(CET .. "/scripting.log", "ab"))
local function log_line(text)
    logf:write("[" .. os.date("%H:%M:%S") .. "] [info] " .. text .. "\n")
    logf:flush()
end

local function load_in(env, code, name)
    if host.setfenv then -- Lua 5.1 / LuaJIT
        local f, err = host.loadstring(code, name)
        if f then host.setfenv(f, env) end
        return f, err
    end
    return host.load(code, name, "t", env)
end

-- Like CET's GetLuaPath: relative paths only, never out of the mod's folder.
local function mod_path(path)
    if type(path) ~= "string" or path == "" or path:match("^[/\\]") or path:match("^%a:") or path:find("%.%.") then return nil end
    return MOD .. "/" .. path:gsub("\\", "/")
end

local events = {}
local env = {}
for _, name in ipairs({ "_VERSION", "assert", "error", "getmetatable", "ipairs", "next", "pairs", "pcall", "rawequal", "rawget",
    "rawlen", "rawset", "select", "setmetatable", "tonumber", "tostring", "type", "unpack", "xpcall", "collectgarbage",
    "string", "table", "math", "bit32" }) do
    env[name] = host[name]
end
env.print = function(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
    log_line(table.concat(parts, " "))
end
env.GetVersion = function() return "v1.38.1-fake" end
env.registerForEvent = function(name, fn) events[name] = fn end
env.loadstring = function(code, name) return load_in(env, code, name) end
local modules = {}
env.require = function(name)
    if modules[name] ~= nil then return modules[name] end
    local path = mod_path(name .. ".lua")
    local f = path and io.open(path, "rb")
    if not f then return nil, "Tried to access invalid path '" .. tostring(name) .. "'!" end
    local code = f:read("*a")
    f:close()
    local chunk, err = load_in(env, code, "@" .. path)
    if not chunk then return nil, err end
    modules[name] = chunk() or true
    return modules[name]
end
env.io = {
    open = function(path, mode)
        local p = mod_path(path)
        if not p then return nil, "invalid path" end
        return io.open(p, mode or "r")
    end,
}
env.os = {
    clock = os.clock, date = os.date, time = os.time,
    remove = function(path) local p = mod_path(path) if not p then return nil, "invalid path" end return os.remove(p) end,
    rename = function(a, b)
        local pa, pb = mod_path(a), mod_path(b)
        if not pa or not pb then return nil, "invalid path" end
        return os.rename(pa, pb)
    end,
}

-- The game, as little of it as player_info and the value display need.
local function game_object(class, methods)
    local index = { GetClassName = function() return { class = class } end }
    for k, v in pairs(methods) do index[k] = v end
    if host.newproxy then -- LuaJIT: a userdata, like CET's handles
        local u = host.newproxy(true)
        getmetatable(u).__index = index
        return u
    end
    return setmetatable({}, { __index = index })
end
local entity_id = host.loadstring and host.loadstring("return 1234567890123ULL")() or 1234567890123
local player = game_object("PlayerPuppet", {
    GetEntityID = function() return entity_id end,
    GetWorldPosition = function() return { x = -1050.25, y = 1340.5, z = 30.0, w = 1.0 } end,
})
local stats = { Level = 57, StreetCred = 50, Health = 500 }
env.gamedataStatType = { Level = "Level", StreetCred = "StreetCred", Health = "Health" }
env.gamedataStatPoolType = { Health = "Health" }

-- TweakDB records, the inventory, targeting and DumpType, shaped the way CET 1.38 hands them over on patch 2.5:
-- out parameters come back as extra return values (success first), DisplayName() is a LocKey CName.
local records = {
    ["Items.money"] = { class = "gamedataItem_Record", name = "Eurodollars" },
    ["Items.Preset_Katana_Saburo"] = { class = "gamedataWeaponItem_Record", name = "Satori" },
    ["Items.w_katana_blade"] = { class = "gamedataItem_Record" },
    ["Vehicle.v_sport1_quadra_turbo_r"] = { class = "gamedataVehicle_Record", name = "Turbo-R V-Tech" },
}
local function record(id)
    local path = type(id) == "table" and id.path or id
    local r = records[path]
    if not r then return nil end
    return game_object(r.class, { GetID = function() return { path = path } end,
        DisplayName = function() return { key = r.name or "None" } end })
end
local function item_data(path) return game_object("gameItemData", { GetID = function() return { tdbid = { path = path } } end }) end
env.TDBID = { ToStringDEBUG = function(id) return type(id) == "table" and id.path or tostring(id) end }
env.TweakDB = {
    GetRecord = function(_, id) return record(id) end,
    GetRecords = function(_, t)
        local paths = {}
        for path, r in pairs(records) do
            if r.class == t or (t == "gamedataItem_Record" and r.class:find("Item_Record$")) then paths[#paths + 1] = path end
        end
        table.sort(paths)
        local out = {}
        for i, path in ipairs(paths) do out[i] = record(path) end
        return out
    end,
    GetFlat = function() return nil end,
}
env.MarketSystem = { Money = function() return { tdbid = { path = "Items.money" } } end }
env.DumpType = function(name)
    local text = "{\n\tname: Unknown,\n\tfunctions: {\n\t},\n\tstaticFunctions: {\n\t},\n\tproperties: {\n\t}\n}"
    if name == "PlayerPuppet" then
        text = "{\n\tname: PlayerPuppet,\n\tfunctions: {\n\t\tIsInCombat() => (Bool),\n\t\tGetLevel(Int32, Bool) => (Float),\n"
            .. "\t},\n\tstaticFunctions: {\n\t},\n\tproperties: {\n\t\tquickSlotsManager: handle:QuickSlotsManager,\n\t}\n}"
    end
    return setmetatable({}, { __tostring = function() return text end })
end
local car = game_object("vehicleCarBaseObject", {
    GetWorldPosition = function() return { x = -1047.25, y = 1336.5, z = 30.0 } end,
    GetDisplayName = function() return "Cortes V5000 Valor" end,
})
local car_part = game_object("gameTargetingComponent", { GetComponent = function() return game_object("x", { GetEntity = function() return car end }) end })
env.GameTime = {
    Days = function(t) return math.floor(t / 86400) end,
    Hours = function(t) return math.floor(t / 3600) % 24 end,
    Minutes = function(t) return math.floor(t / 60) % 60 end,
}
env.Game = {
    GetPlayer = function() return player end,
    GetStatsSystem = function() return { GetStatValue = function(_, id, stat) return stats[stat] or 0 end } end,
    GetStatPoolsSystem = function() return { GetStatPoolValue = function(_, id, pool, perc) return perc and 89.5 or 432.5 end } end,
    GetTransactionSystem = function()
        return {
            GetItemList = function() return true, { item_data("Items.money"), item_data("Items.Preset_Katana_Saburo") } end,
            GetItemQuantity = function(_, _, id) return id.tdbid.path == "Items.money" and 340925 or 1 end,
        }
    end,
    GetTargetingSystem = function() return { GetTargetParts = function() return true, { car_part } end } end,
    ["TSQ_ALL;"] = function() return {} end,
    GetTimeSystem = function() return { GetGameTime = function() return 76 * 86400 + 15 * 3600 + 46 * 60 end } end,
    GetLocalizedTextByKey = function(key) return type(key) == "table" and key.key or "" end,
    NameToString = function(cn) return type(cn) == "table" and cn.class or nil end,
}
env.FakeEntityID = function() return entity_id end

local f = assert(io.open(MOD .. "/init.lua", "rb"))
local main = assert(load_in(env, f:read("*a"), "@" .. MOD .. "/init.lua"))
f:close()
main()
if events.onInit then events.onInit() end

local t0, last = os.clock(), os.clock()
while os.clock() - t0 < seconds do
    local stop = io.open(CET .. "/STOP")
    if stop then stop:close() break end
    local now = os.clock()
    if now - last >= 0.005 then
        if events.onUpdate then
            local ok, err = pcall(events.onUpdate, now - last)
            if not ok then log_line("onUpdate error: " .. tostring(err)) end
        end
        last = now
    end
end
