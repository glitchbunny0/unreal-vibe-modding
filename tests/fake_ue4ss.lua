--[[ Runs the real VibeBridge main.lua outside a game: a single-threaded stand-in for UE4SS's LoopAsync and
     ExecuteInGameThread, printing to UE4SS.log. Stops after SECONDS or when <ue4ss>/STOP appears.
     lua tests/fake_ue4ss.lua <ue4ss folder> [SECONDS]

     For the watch tests: RegisterHook/UnregisterHook keep hooks in a list, FakeFire(path, self_name, ...) calls
     them the way UE4SS does (every argument wrapped, read with :get()), FakeObject(name) is a UObject stand-in,
     and FakeLater(ms, fn) runs fn on the "game thread" later. ]]
local U, seconds = arg[1], tonumber(arg[2] or "30")
local loops, queue, timers = {}, {}, {}
local hooks, next_hook = {}, 1

local function fake_param(v) return setmetatable({}, { __index = { get = function() return v end } }) end

function FakeObject(name, valid)
    return setmetatable({}, { __name = "UObject", __index = {
        IsValid = function() return valid ~= false end,
        GetFName = function() return { ToString = function() return name end } end,
        GetFullName = function() return "FakeClass /Game/Fake." .. name end,
    } })
end

function RegisterHook(path, pre, post)
    if type(path) ~= "string" or not path:find(":", 1, true) then error("No UFunction named '" .. tostring(path) .. "'", 2) end
    local id = next_hook
    next_hook = next_hook + 1
    if path:find("_C:", 1, true) then
        -- Like UE4SS for a Blueprint function: the post callback is dropped, and pre and post share one id.
        hooks[#hooks + 1] = { path = path, pre = pre, id = id, post_id = id }
        return id, id
    end
    hooks[#hooks + 1] = { path = path, pre = pre, post = post, id = id, post_id = id + 1000 }
    return id, id + 1000
end

function UnregisterHook(path, pre, post)
    for i = #hooks, 1, -1 do
        if hooks[i].path == path and hooks[i].id == pre and hooks[i].post_id == post then
            table.remove(hooks, i)
            return
        end
    end
    error("no hook " .. tostring(pre) .. " on " .. tostring(path), 2)
end

function FakeHookCount() return #hooks end

function FakeFire(path, self_name, ...)
    local n, fired = select("#", ...), 0
    for _, h in ipairs({ table.unpack(hooks) }) do
        if h.path == path then
            fired = fired + 1
            local args = { fake_param(FakeObject(self_name)) }
            for i = 1, n do args[i + 1] = fake_param((select(i, ...))) end
            if h.pre then h.pre(table.unpack(args, 1, n + 1)) end
            if h.post then h.post(table.unpack(args, 1, n + 1)) end
        end
    end
    return fired
end

function FakeLater(ms, fn) timers[#timers + 1] = { at = os.clock() + ms / 1000, fn = fn } end

function LoopAsync(ms, f) loops[#loops + 1] = { ms = ms, f = f, next = 0 } end
function ExecuteInGameThread(f) queue[#queue + 1] = f end
local logf = assert(io.open(U .. "/UE4SS.log", "ab"))
print = function(...)
    local parts = {}
    for i = 1, select("#", ...) do parts[i] = tostring((select(i, ...))) end
    logf:write("[Lua] " .. table.concat(parts, "\t"))
    logf:flush()
end
package.path = U .. "/Mods/VibeBridge/Scripts/?.lua;" .. package.path
assert(loadfile((arg[0]:match("^(.*)[/\\]") or ".") .. "/fake_reflection.lua"))()
assert(loadfile(U .. "/Mods/VibeBridge/Scripts/main.lua"))()

local t0 = os.clock()
while os.clock() - t0 < seconds do
    local stop = io.open(U .. "/STOP")
    if stop then stop:close() break end
    local now = (os.clock() - t0) * 1000
    for _, l in ipairs(loops) do
        if not l.dead and now >= l.next then
            l.next = now + l.ms
            if l.f() then l.dead = true end
        end
    end
    for i = #timers, 1, -1 do
        if os.clock() >= timers[i].at then queue[#queue + 1] = table.remove(timers, i).fn end
    end
    local q = queue
    queue = {}
    for _, f in ipairs(q) do
        local ok, err = pcall(f)
        if not ok then print("game thread error: " .. tostring(err) .. "\n") end
    end
end
