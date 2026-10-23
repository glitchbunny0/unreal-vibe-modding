--[[ Runs the real VibeBridge main.lua outside a game: a single-threaded stand-in for UE4SS's LoopAsync and
     ExecuteInGameThread, printing to UE4SS.log. Stops after SECONDS or when <ue4ss>/STOP appears.
     lua tests/fake_ue4ss.lua <ue4ss folder> [SECONDS] ]]
local U, seconds = arg[1], tonumber(arg[2] or "30")
local loops, queue = {}, {}
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
    local q = queue
    queue = {}
    for _, f in ipairs(q) do
        local ok, err = pcall(f)
        if not ok then print("game thread error: " .. tostring(err) .. "\n") end
    end
end
