--[[ {{NAME}}: the hot-swap shell. Made by `uvmod new`; you rarely need to change this file.

    UE4SS runs this file once, at game start. It registers every hook, keybind and the one loop, and each of them
    only forwards into the logic table that logic.lua returns. All mod state (S) lives here.

    Why: UE4SS's own hot reload tears a mod's Lua state down while the game still holds hooks into it, and the next
    hooked call can hang or crash the game. Here nothing is torn down: `uvmod swap {{NAME}}` copies the files and
    drops hotswap/req; on the next tick the shell runs logic.lua again in THIS state, checks it and swaps the table.
    A logic file that does not compile, throws while loading, registers anything itself or breaks the contract is
    refused, and the running logic stays. A change to THIS file needs a game restart (`uvmod restart`).

    Contract (SHELL = 1): logic.lua returns a table with
      SHELL = 1, VERSION = "...", init(S), tick(), on_key(key), optional on_swap(info),
      KEYS  = { { key = "<UE4SS Key name>", label = "..." }, ... }
      HOOKS = { { path = "<UFunction path>", handler = "<name of a function in the table>" }, ... }
--]]

local SHELL = 1
local NAME = "{{NAME}}"
local TICK_MS = 100
local HOOK_RETRY_TICKS = 50 -- a Blueprint class may not be loaded yet (main menu): retry every 5 s
local MAX_ERRORS = 3        -- logged per handler per generation

local function log(fmt, ...)
    print(string.format("[" .. NAME .. "] " .. fmt .. "\n", ...))
end

local function read(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local s = f:read("a")
    f:close()
    return s
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

local DIR = (debug.getinfo(1, "S").source or ""):match("^@(.*[/\\])main%.lua$") or ("Mods\\" .. NAME .. "\\Scripts\\")
local SEP = DIR:sub(-1)
local SWAP_DIR = (DIR:gsub("Scripts[/\\]$", "")) .. "hotswap" .. SEP
local SHELL_SRC = read(DIR .. "main.lua")

local S = {}       -- the mod's state: owned here, survives every swap
local L = nil      -- the logic table now live
local gen = 0      -- generation: 1 = loaded at start, +1 per swap
local routes = {}  -- hook path -> handler name in L
local errors = {}  -- handler name -> errors this generation
local hooked = {}  -- hook path -> true once registered (never unregistered)
local pending = {} -- hook path -> true while its class is not loaded
local bound = {}   -- key name -> true once bound
local ticks, tick_errors = 0, 0

local function dispatch(name, ...)
    local f = L and L[name]
    if type(f) ~= "function" then return end
    local ok, err = pcall(f, ...)
    if not ok then
        local n = (errors[name] or 0) + 1
        errors[name] = n
        if n <= MAX_ERRORS then log("%s failed (generation %d): %s", name, gen, tostring(err)) end
    end
end

-- A fresh environment per load: globals resolve to UE4SS's, but every registration function raises, so the logic
-- cannot register a hook or loop that a later swap would orphan.
local function logic_env()
    local env = setmetatable({}, { __index = _G })
    local function refuse(k)
        return function()
            error(k .. " called from swappable logic: only main.lua registers (it survives swaps); declare it in HOOKS / KEYS", 2)
        end
    end
    for k in pairs(_G) do
        if type(k) == "string" and k:match("^Register") then env[k] = refuse(k) end
    end
    for _, k in ipairs({ "UnregisterHook", "LoopAsync", "NotifyOnNewObject" }) do env[k] = refuse(k) end
    return env
end

-- Read and run Scripts/logic.lua. Not require: UE4SS's require caches modules, so a second one returns old code.
local function load_logic()
    local src = read(DIR .. "logic.lua")
    if not src then return nil, "logic.lua: cannot read it in " .. DIR end
    local fn, err = load(src, "@logic.lua", "t", logic_env())
    if not fn then return nil, tostring(err) end
    local ok, g = pcall(fn)
    if not ok then return nil, tostring(g) end
    if type(g) ~= "table" then return nil, "logic.lua returned " .. type(g) .. ", not a table" end
    local p = {}
    if g.SHELL ~= SHELL then
        return nil, string.format("logic.lua is written for shell %s, this is shell %d: restart the game", tostring(g.SHELL), SHELL)
    end
    if type(g.VERSION) ~= "string" then p[#p + 1] = "VERSION missing" end
    for _, f in ipairs({ "init", "tick", "on_key" }) do
        if type(g[f]) ~= "function" then p[#p + 1] = f .. "() missing" end
    end
    for i, a in ipairs(type(g.KEYS) == "table" and g.KEYS or {}) do
        if type(a) ~= "table" or type(a.key) ~= "string" then p[#p + 1] = "KEYS[" .. i .. "] has no key name" end
    end
    for i, h in ipairs(type(g.HOOKS) == "table" and g.HOOKS or {}) do
        if type(h) ~= "table" or type(h.path) ~= "string" then p[#p + 1] = "HOOKS[" .. i .. "] has no path"
        elseif type(g[h.handler]) ~= "function" then p[#p + 1] = "HOOKS[" .. i .. "] handler " .. tostring(h.handler) .. "() missing" end
    end
    if type(g.KEYS) ~= "table" then p[#p + 1] = "KEYS missing" end
    if type(g.HOOKS) ~= "table" then p[#p + 1] = "HOOKS missing" end
    if #p > 0 then return nil, "logic.lua: " .. table.concat(p, "; ") end
    return g
end

local function activate(g)
    local ok, err = pcall(g.init, S)
    if not ok then return false, "init(S) failed: " .. tostring(err) end
    local r = {}
    for _, h in ipairs(g.HOOKS) do r[h.path] = h.handler end
    L, routes, errors, gen = g, r, {}, gen + 1
    return true
end

local function register_hooks()
    local new = {}
    for _, h in ipairs(L and L.HOOKS or {}) do
        local path = h.path
        if not hooked[path] then
            local ok = pcall(RegisterHook, path, function(...)
                local name = routes[path]
                if name then dispatch(name, ...) end
            end)
            if ok then
                hooked[path], pending[path] = true, nil
                new[#new + 1] = path
                log("hooked: %s", path)
            else
                pending[path] = true
            end
        end
    end
    return new
end

local function bind_keys()
    local new = {}
    for _, a in ipairs(L and L.KEYS or {}) do
        local key = a.key
        if not bound[key] then
            local k = Key and Key[key]
            if k == nil then
                log("WARN key %s is not in this UE4SS build; %s stays unbound", key, a.label or key)
            elseif pcall(RegisterKeyBind, k, function() ExecuteInGameThread(function() dispatch("on_key", key) end) end) then
                bound[key] = true
                new[#new + 1] = key
            end
        end
    end
    return new
end

local function swap()
    local old = L and L.VERSION or "no logic"
    local g, err = load_logic()
    if g then
        local ok
        ok, err = activate(g)
        if not ok then g = nil end
    end
    if not g then
        log("swap REFUSED: %s", err)
        return string.format("REFUSED: %s\nstill running %s (generation %d)\n", err, old, gen)
    end
    local hooks_new, keys_new = register_hooks(), bind_keys()
    local lines = { string.format("OK %s -> %s, generation %d", old, L.VERSION, gen) }
    if #hooks_new > 0 then lines[#lines + 1] = "new hooks: " .. table.concat(hooks_new, ", ") end
    if #keys_new > 0 then lines[#lines + 1] = "new keys: " .. table.concat(keys_new, " ") end
    if next(pending) then
        local w = {}
        for path in pairs(pending) do w[#w + 1] = path end
        lines[#lines + 1] = "waiting for their class to load: " .. table.concat(w, ", ")
    end
    if read(DIR .. "main.lua") ~= SHELL_SRC then
        lines[#lines + 1] = "WARN main.lua on disk differs from the running shell: shell changes need a game restart"
    end
    log("swapped logic %s -> %s (generation %d)", old, L.VERSION, gen)
    dispatch("on_swap", { from = old, to = L.VERSION, generation = gen })
    return table.concat(lines, "\n") .. "\n"
end

-- A request is hotswap/req holding an id; the answer goes to hotswap/<id>.out (uvmod swap writes and reads them).
local function check_swap()
    local req = read(SWAP_DIR .. "req")
    if req == nil then return end
    os.remove(SWAP_DIR .. "req")
    local id = req:match("^%s*([%w_%-%.]+)") or ("anon" .. ticks)
    if not write_atomic(SWAP_DIR .. id .. ".out", swap()) then log("swap %s: cannot write the answer", id) end
end

local function on_tick()
    ticks = ticks + 1
    check_swap()
    if next(pending) ~= nil and ticks % HOOK_RETRY_TICKS == 0 then register_hooks() end
    dispatch("tick")
end

do
    local g, err = load_logic()
    if g then
        local ok
        ok, err = activate(g)
        if not ok then g = nil end
    end
    if g then
        bind_keys()
        register_hooks()
    else
        log("ERROR the logic did not load: %s", err)
    end
    LoopAsync(TICK_MS, function()
        ExecuteInGameThread(function()
            local ok, e = pcall(on_tick)
            if not ok and tick_errors < MAX_ERRORS then
                tick_errors = tick_errors + 1
                log("tick failed: %s", tostring(e))
            end
        end)
        return false
    end)
    if L then
        log("v%s loaded (hot-swap shell %d)", L.VERSION, SHELL)
    else
        log("hot-swap shell %d loaded WITHOUT logic (error above): fix logic.lua and run `uvmod swap %s`", SHELL, NAME)
    end
end
