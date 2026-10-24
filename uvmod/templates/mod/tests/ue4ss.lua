--[[ A strict stand-in for UE4SS, for testing {{NAME}} without the game: `lua tests/test_mod.lua` from the project
    folder. Made by `uvmod new`; `uvmod mock` refreshes tests/game_api.lua (the real signatures of the UFunctions
    the mod calls, read from the game's reflection index).

    It enforces the call rules that crash the real game instead of raising a Lua error: a UFunction parameter of
    type FName gets anything but FName("...") -> NATIVE CRASH (recorded in mock.crashes, and the call raises), and
    a wrong argument count raises the Lua error UE4SS raises. Out parameters count as arguments (pass {}).
--]]

local mock = { logs = {}, crashes = {}, keys = {}, hooks = {}, loops = {}, objects = {}, by_path = {} }

local ok_api, API = pcall(dofile, "tests/game_api.lua")
mock.api = ok_api and API or {}

-- ------------------------------------------------------------------------------------------------ values
local FNAME = {}
FNAME.__index = { ToString = function(self) return self[1] end, type = function() return "FName" end }
FNAME.__name = "FName"
FNAME.__eq = function(a, b) return a[1] == b[1] end
function FName(s)
    if type(s) ~= "string" then error("FName(...) needs a string", 2) end
    return setmetatable({ s }, FNAME)
end
local function is_fname(v) return type(v) == "table" and getmetatable(v) == FNAME end

local function crash(msg)
    mock.crashes[#mock.crashes + 1] = msg
    error("NATIVE CRASH (the real game would die here): " .. msg, 3)
end

-- Check a call against the signatures in tests/game_api.lua: { {name, kind, role}, ... } per candidate.
local function check_call(name, args)
    local candidates = mock.api[name]
    if not candidates then return end
    local arity_ok = false
    for _, params in ipairs(candidates) do
        local inputs = {}
        for _, p in ipairs(params) do if p.role ~= "ret" then inputs[#inputs + 1] = p end end
        if #inputs == args.n then
            arity_ok = true
            for i, p in ipairs(inputs) do
                if p.kind == "NameProperty" and not is_fname(args[i]) then
                    crash(string.format("%s argument %d (%s) is an FName, got %s", name, i, p.name, type(args[i])))
                end
            end
        end
    end
    if not arity_ok then
        local counts = {}
        for _, params in ipairs(candidates) do
            local n = 0
            for _, p in ipairs(params) do if p.role ~= "ret" then n = n + 1 end end
            counts[#counts + 1] = tostring(n)
        end
        error(string.format("[UFunction::setup_metamethods -> __call] %s expected %s parameters, received %d", name,
            table.concat(counts, " or "), args.n), 3)
    end
end

-- ------------------------------------------------------------------------------------------------ objects
-- mock.object{ path = "/Game/Maps/M.M:PersistentLevel.BP_Thing_C_0", class = "BP_Thing_C", supers = { "Actor" },
--              props = { Health = 100 }, methods = { K2_GetActorLocation = function(self) return {X=0,Y=0,Z=0} end } }
function mock.object(spec)
    local state = { valid = true, props = spec.props or {} }
    local o
    local builtin = {
        IsValid = function() return state.valid end,
        GetFullName = function() return (spec.class or "Object") .. " " .. (spec.path or "/Game/Fake") end,
        GetFName = function() return FName((spec.path or "Fake"):match("[.:]([^.:]+)$") or "Fake") end,
        type = function() return "UObject" end,
        Destroy = function() state.valid = false end, -- for tests: the game destroyed it
    }
    o = setmetatable({}, {
        __name = "UObject",
        __index = function(_, k)
            if builtin[k] then return builtin[k] end
            if not state.valid then crash("read '" .. tostring(k) .. "' on an object the game destroyed") end
            if state.props[k] ~= nil then return state.props[k] end
            local m = spec.methods and spec.methods[k]
            if m then
                return function(self, ...)
                    check_call(k, table.pack(...))
                    return m(self, ...)
                end
            end
            return nil
        end,
        __newindex = function(_, k, v) state.props[k] = v end,
    })
    local classes = { spec.class or "Object" }
    for _, s in ipairs(spec.supers or {}) do classes[#classes + 1] = s end
    mock.objects[#mock.objects + 1] = { obj = o, classes = classes }
    if spec.path then mock.by_path[spec.path] = o end
    return o
end

local INVALID = setmetatable({}, { __index = { IsValid = function() return false end, type = function() return "UObject" end } })

function FindFirstOf(class)
    for _, e in ipairs(mock.objects) do
        for _, c in ipairs(e.classes) do
            if c == class and e.obj:IsValid() then return e.obj end
        end
    end
    return INVALID
end

function FindAllOf(class)
    local out = {}
    for _, e in ipairs(mock.objects) do
        for _, c in ipairs(e.classes) do
            if c == class then out[#out + 1] = e.obj break end
        end
    end
    return #out > 0 and out or nil
end

function StaticFindObject(path) return mock.by_path[path] or INVALID end

-- ------------------------------------------------------------------------------------------------ UE4SS globals
Key = setmetatable({}, { __index = function(_, k) return k end })
function RegisterKeyBind(key, fn) mock.keys[key] = fn end
function RegisterHook(path, pre, post)
    if type(path) ~= "string" or not path:find(":", 1, true) then error("No UFunction named " .. tostring(path), 2) end
    mock.hooks[path] = { pre = pre, post = post }
    return #mock.hooks + 1, #mock.hooks + 1
end
function UnregisterHook(path) mock.hooks[path] = nil end
function LoopAsync(ms, fn) mock.loops[#mock.loops + 1] = fn end
function ExecuteInGameThread(fn) fn() end
function NotifyOnNewObject() end
print = function(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
    mock.logs[#mock.logs + 1] = table.concat(parts, "\t"):gsub("\n$", "")
end

-- ------------------------------------------------------------------------------------------------ driving
function mock.load_mod(name) assert(loadfile(name .. "/Scripts/main.lua"))() end
function mock.press(key)
    assert(mock.keys[key], "no keybind for " .. key)
    mock.keys[key]()
end
function mock.tick(n)
    for _ = 1, n or 1 do
        for _, fn in ipairs(mock.loops) do fn() end
    end
end
-- Call a hooked function the way UE4SS does: every argument wrapped, read with :get().
function mock.fire(path, self_obj, ...)
    local h = assert(mock.hooks[path], "nothing hooked " .. path)
    local function wrap(v) return { get = function() return v end, set = function() end } end
    local args = { wrap(self_obj) }
    for i = 1, select("#", ...) do args[#args + 1] = wrap((select(i, ...))) end
    if h.pre then h.pre(table.unpack(args)) end
end
function mock.last_log() return mock.logs[#mock.logs] or "" end
function mock.find_log(text)
    for _, l in ipairs(mock.logs) do if l:find(text, 1, true) then return l end end
end

-- A tiny test runner.
local passed, failed = 0, 0
function mock.check(name, cond, detail)
    if cond then passed = passed + 1 else
        failed = failed + 1
        io.stderr:write("FAIL " .. name .. (detail ~= nil and (": " .. tostring(detail)) or "") .. "\n")
    end
end
function mock.done()
    io.stdout:write(string.format("%d passed, %d failed\n", passed, failed))
    os.exit(failed == 0 and 0 or 1)
end

return mock
