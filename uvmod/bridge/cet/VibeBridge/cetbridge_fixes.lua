--[[ Fixes and additions for CETBridge's game handlers (handlers.lua, kept unmodified beside this file) on Cyberpunk 2077
     2.5 with CET 1.38. init.lua passes the handlers table through this module, which replaces the handlers that fail
     there and adds `overview`. Each replacement says what was wrong. Checked live on patch 2.5 (2026-10-28).
     Same MIT terms as handlers.lua; see LICENSE.CETBridge. ]]

local function try(f, ...)
    local ok, v = pcall(f, ...)
    if ok then return v end
end

-- A record's display name. DisplayName() is a CName that holds a LocKey hash, so GetLocalizedText (which wants a
-- string) gives "", GetLocalizedTextByKey gives the name.
local function display_name(record)
    local key = try(function() return record:DisplayName() end)
    if key == nil then return nil end
    -- A record without a name has the key None, which comes back as the text "None".
    local text = try(function() return Game.GetLocalizedTextByKey(key) end)
    if type(text) == "string" and text ~= "" and text ~= "None" then return text end
    text = try(function() return Game.GetLocalizedText(key.value) end)
    if type(text) == "string" and text ~= "" and text ~= "None" then return text end
end

local function localized(text)
    local out = try(function() return Game.GetLocalizedText(text) end)
    if type(out) == "string" and out ~= "" then return out end
    return text
end

local function name_string(cname)
    local text = try(function() return Game.NameToString(cname) end)
    if type(text) == "string" and text ~= "" then return text end
    return try(function() return cname.value end)
end

-- "WeaponItem" or "WeaponItem_Record" -> "gamedataWeaponItem_Record"; full names stay.
local function record_type(name)
    if name:find("^gamedata") then return name end
    return "gamedata" .. name:gsub("_Record$", "") .. "_Record"
end

-- CET's DumpType(name) returns a descriptor whose fields Lua cannot read; its text lists them one per line.
local function parse_descriptor(text)
    local out = { name = text:match("name: ([^,\n]+)"), functions = {}, staticFunctions = {}, properties = {} }
    local section
    for line in text:gmatch("[^\n]+") do
        local key = line:match("^\t(%a+): {")
        if key then
            section = out[key]
        elseif line:match("^\t}") then
            section = nil
        elseif section then
            local entry = line:match("^\t\t(.-),?$")
            if entry and entry ~= "" then section[#section + 1] = entry end
        end
    end
    return out
end

local ZONES = { [-1] = "Any", [0] = "Default", [1] = "Public", [2] = "Safe", [3] = "Restricted", [4] = "Dangerous" }

local function psm_int(player, field)
    local defs = GetAllBlackboardDefs().PlayerStateMachine
    local bb = Game.GetBlackboardSystem():GetLocalInstanced(player:GetEntityID(), defs)
    return bb:GetInt(defs[field])
end

return function(handlers)
    -- The player's health came back as a percentage (GetStatPoolValue without its `perc` argument) beside an
    -- absolute maximum. Now: health absolute, healthPercent beside it.
    local player_info = handlers.player_info
    handlers.player_info = function(args)
        local info, err = player_info(args)
        if not info then return info, err end
        local player = Game.GetPlayer()
        local pools = Game.GetStatPoolsSystem()
        local abs = try(function() return pools:GetStatPoolValue(player:GetEntityID(), gamedataStatPoolType.Health, false) end)
        local pct = try(function() return pools:GetStatPoolValue(player:GetEntityID(), gamedataStatPoolType.Health, true) end)
        if abs then info.health = math.floor(abs) end -- floored like maxHealth, so full health reads health == maxHealth
        if pct then info.healthPercent = math.floor(pct) end
        return info
    end

    -- Weather and zone were always "unknown": 2.5's weather system has no GetWeatherState, and there is no
    -- Game.GetPreventionSystem. Now: the rain from the weather system and the zone from the player's state machine.
    local game_state = handlers.game_state
    handlers.game_state = function(args)
        local state, err = game_state(args)
        if not state then return state, err end
        state.weather, state.zoneType = nil, nil
        local rain = try(function() return tostring(Game.GetWeatherSystem():GetRainIntensityType()) end)
        state.rain = rain and (rain:match(": (%a+) %(") or rain) or nil
        local player = Game.GetPlayer()
        if player then
            local zone = try(psm_int, player, "Zones")
            state.zone = zone and (ZONES[zone] or tostring(zone)) or nil
        end
        return state
    end

    -- GetItemList returns (success, items) in CET; the list was read from the first value, a boolean. Names now come
    -- from the record's LocKey.
    handlers.get_inventory = function(args)
        local player = Game.GetPlayer()
        if not player then return nil, "Player not available" end
        local limit = (args and args.limit) or 50
        local filter = args and args.type
        local ts = Game.GetTransactionSystem()
        local _, list = ts:GetItemList(player)
        if type(list) ~= "table" then return nil, "the transaction system returned no item list" end
        local items, total = {}, #list
        for _, data in ipairs(list) do
            if #items >= limit then break end
            local id = data:GetID()
            local record = try(function() return TweakDB:GetRecord(id.tdbid) end)
            local class = record and name_string(record:GetClassName()) or "Unknown"
            if not filter or (class and class:find(filter, 1, true)) then
                items[#items + 1] = {
                    id = TDBID.ToStringDEBUG(id.tdbid),
                    type = class,
                    name = record and display_name(record) or nil,
                    quantity = tonumber(ts:GetItemQuantity(player, id)),
                }
            end
        end
        return { count = #items, total = total, items = items }
    end

    -- GetTargetParts returns (success, parts) in CET, the same mistake as get_inventory.
    local function target_parts(player, query_name, radius)
        local query = Game[query_name]()
        query.maxDistance = radius
        local _, parts = Game.GetTargetingSystem():GetTargetParts(player, query)
        return type(parts) == "table" and parts or {}
    end

    handlers.get_nearby_entities = function(args)
        local player = Game.GetPlayer()
        if not player then return nil, "Player not available" end
        local radius = (args and args.radius) or 20
        local filter = args and args.type
        local limit = (args and args.limit) or 20
        local here = player:GetWorldPosition()
        local entities = {}
        for _, part in ipairs(target_parts(player, "TSQ_ALL;", radius)) do
            if #entities >= limit then break end
            local entity = try(function() return part:GetComponent():GetEntity() end)
            if entity then
                local class = name_string(entity:GetClassName()) or "Unknown"
                local kind = "Unknown"
                if class:find("NPC") or class:find("Puppet") then kind = "NPC"
                elseif class:lower():find("vehicle") then kind = "Vehicle"
                elseif class:lower():find("item") then kind = "Item"
                elseif class:lower():find("device") then kind = "Device" end
                if not filter or kind == filter then
                    local pos = entity:GetWorldPosition()
                    local dx, dy, dz = pos.x - here.x, pos.y - here.y, pos.z - here.z
                    local name = try(function() return entity:GetDisplayName() end)
                    entities[#entities + 1] = {
                        className = class, type = kind, name = (name ~= "" and name) or nil,
                        distance = math.floor(math.sqrt(dx * dx + dy * dy + dz * dz) * 10) / 10,
                        position = { x = pos.x, y = pos.y, z = pos.z },
                    }
                end
            end
        end
        return { radius = radius, count = #entities, entities = entities }
    end

    handlers.kill_nearby_npcs = function(args)
        local player = Game.GetPlayer()
        if not player then return nil, "Player not available" end
        local radius = (args and args.radius) or 30
        local all = args and args.allNpcs or false
        local killed = 0
        for _, part in ipairs(target_parts(player, "TSQ_NPC;", radius)) do
            local npc = try(function() return part:GetComponent():GetEntity() end)
            if npc then
                local hostile = all or try(function()
                    return npc:GetAttitudeAgent():GetAttitudeTowards(player:GetAttitudeAgent()) == EAIAttitude.AIA_Hostile
                end)
                if hostile and pcall(function() npc:Kill(player, false, false) end) then killed = killed + 1 end
            end
        end
        return { killed = killed, radius = radius, allNpcs = all }
    end

    -- Vehicle names came back as unresolved CNames (same LocKey mistake as the inventory).
    handlers.get_vehicle_list = function()
        local list = Game.GetVehicleSystem():GetPlayerUnlockedVehicles()
        local vehicles = {}
        for _, data in ipairs(list or {}) do
            local record = try(function() return TweakDB:GetRecord(data.recordID) end)
            vehicles[#vehicles + 1] = { id = TDBID.ToStringDEBUG(data.recordID), name = record and display_name(record) or nil }
        end
        return { count = #vehicles, vehicles = vehicles }
    end

    -- TweakDB:GetRecords needs a record type; called without one it returned nothing. Now `type` picks one (short
    -- names work: WeaponItem, Vehicle) and without it the common ones are searched.
    handlers.search_tweakdb = function(args)
        if not args or not args.pattern then return nil, "pattern is required" end
        local pattern = string.lower(args.pattern)
        local limit = args.limit or 20
        local types = args.type and { record_type(args.type) }
            or { "gamedataItem_Record", "gamedataVehicle_Record", "gamedataCharacter_Record", "gamedataStatusEffect_Record" }
        local results, searched = {}, 0
        for _, t in ipairs(types) do
            local records = TweakDB:GetRecords(t)
            if records == nil and args.type then return nil, "no record type " .. t end
            for _, record in ipairs(records or {}) do
                if #results >= limit then break end
                searched = searched + 1
                local path = try(function() return TDBID.ToStringDEBUG(record:GetID()) end)
                if path and path:lower():find(pattern, 1, true) then
                    results[#results + 1] = { path = path, type = t, name = display_name(record) }
                end
            end
        end
        return { pattern = args.pattern, count = #results, searched = searched, results = results }
    end

    -- There is no Game.GetRTTISystem; CET's own DumpType describes a type. `pattern` keeps only matching entries
    -- (PlayerPuppet alone has about 1,500).
    handlers.dump_type = function(args)
        if not args or not args.typeName then return nil, "No type name provided" end
        local d = parse_descriptor(tostring(DumpType(args.typeName, false)))
        if d.name == nil or d.name == "Unknown" then return nil, "Type not found: " .. args.typeName end
        if args.pattern then
            local p = string.lower(args.pattern)
            for _, key in ipairs({ "functions", "staticFunctions", "properties" }) do
                local kept = {}
                for _, entry in ipairs(d[key]) do
                    if entry:lower():find(p, 1, true) then kept[#kept + 1] = entry end
                end
                d[key] = kept
            end
        end
        return d
    end

    -- For a record, `flats` stayed empty: CET has no TweakDB:GetRecordFlats. Now the record type's getters (from
    -- DumpType) name its flats, read with TweakDB:GetFlat("<path>.<field>"), plus the display name.
    local tweakdb_get = handlers.tweakdb_get
    handlers.tweakdb_get = function(args)
        local info, err = tweakdb_get(args)
        if not info or info.type ~= "record" then return info, err end
        info.flats = nil
        local record = try(function() return TweakDB:GetRecord(args.path) end)
        info.name = record and display_name(record) or nil
        local d = parse_descriptor(tostring(DumpType(info.className, false)))
        local fields, count = {}, 0
        for _, fn in ipairs(d.functions) do
            local getter = fn:match("^(%w+)%(%) => ")
            if getter and not getter:find("Handle$") and count < 200 then
                local field = getter:sub(1, 1):lower() .. getter:sub(2)
                local value = try(function() return TweakDB:GetFlat(args.path .. "." .. field) end)
                if value ~= nil then
                    fields[field] = value
                    count = count + 1
                end
            end
        end
        info.fields = fields
        return info
    end

    -- 2.5's weather system offers no way to set the weather to scripts (no SetWeather), so say so instead of failing.
    handlers.set_weather = function()
        return nil, "not possible on this game version: the weather system has no SetWeather in the script API"
    end

    -- New: the basics of where the player is and what they are doing, in one call.
    handlers.overview = function()
        local p = Game.GetPlayer()
        if not p then return nil, "Player not available (main menu or loading)" end
        local o = {}
        o.level = try(function() return math.floor(Game.GetStatsSystem():GetStatValue(p:GetEntityID(), gamedataStatType.Level)) end)
        o.eddies = try(function() return tonumber(Game.GetTransactionSystem():GetItemQuantity(p, MarketSystem.Money())) end)
        o.position = try(function()
            local v = p:GetWorldPosition()
            return { x = math.floor(v.x * 10) / 10, y = math.floor(v.y * 10) / 10, z = math.floor(v.z * 10) / 10 }
        end)
        o.time = try(function()
            local t = Game.GetTimeSystem():GetGameTime()
            return string.format("day %d, %02d:%02d", GameTime.Days(t), GameTime.Hours(t), GameTime.Minutes(t))
        end)
        local prevention = try(function() return Game.GetScriptableSystemsContainer():Get("PreventionSystem") end)
        if prevention then
            o.district = try(function() return localized(prevention:GetCurrentDistrict():GetDistrictRecord():LocalizedName()) end)
            o.wanted = try(function() return EnumInt(prevention:GetHeatStage()) end)
        end
        o.zone = try(function() local z = psm_int(p, "Zones") return ZONES[z] or tostring(z) end)
        o.vehicle = try(function()
            local v = Game.GetMountedVehicle(p)
            if not v then return false end
            return display_name(v:GetRecord()) or name_string(v:GetClassName())
        end)
        o.quest = try(function()
            local journal = Game.GetJournalManager()
            local objective = journal:GetTrackedEntry()
            if not objective then return nil end
            local q = { objective = localized(objective:GetDescription()) }
            local entry = objective
            for _ = 1, 8 do
                entry = journal:GetParentEntry(entry)
                if not entry then break end
                if entry:IsA("gameJournalQuest") then q.title = localized(entry:GetTitle(journal)) end
            end
            return q
        end)
        return o
    end

    return handlers
end
