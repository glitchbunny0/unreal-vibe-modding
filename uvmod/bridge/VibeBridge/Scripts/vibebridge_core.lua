--[[ VibeBridge core: the LuaRepl protocol (docs/protocol.md) in plain Lua, so it runs and is tested without UE4SS.
     main.lua wires it to UE4SS (folder polling, the game thread). ]]

local M = {}

M.PROTOCOL = 1
M.MAX_LIMIT_MS = 5000
M.DEFAULT_LIMIT_MS = 250
-- Plain Lua has no fine wall clock inside a game (os.clock sums CPU time over all threads; os.time has whole
-- seconds), so the limit is an instruction budget, with os.time as a backstop for slow native calls.
M.INSTRUCTIONS_PER_MS = 100000
M.MAX_VALUES, M.MAX_VALUE_LENGTH = 64, 4096
M.MAX_PRINT_BYTES, M.MAX_PRINTS, M.MAX_ERROR_LENGTH = 64 * 1024, 10000, 16 * 1024
M.MAX_TABLE_ENTRIES_COUNTED = 100

---------------------------------------------------------------------------------------------------- JSON

local function decode_error(msg, pos) error({ json = true, msg = msg .. " at byte " .. pos }, 0) end

local escapes = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }

local function skip(s, i)
    local _, e = s:find("^[ \t\r\n]*", i)
    return e + 1
end

local decode_value

local function decode_string(s, i) -- s:sub(i, i) == '"'
    local out, j = {}, i + 1
    while true do
        local c = s:sub(j, j)
        if c == "" then decode_error("unterminated string", i) end
        if c == '"' then return table.concat(out), j + 1 end
        if c == "\\" then
            local e = s:sub(j + 1, j + 1)
            if escapes[e] then
                out[#out + 1] = escapes[e]
                j = j + 2
            elseif e == "u" then
                local hex = s:sub(j + 2, j + 5)
                if not hex:match("^%x%x%x%x$") then decode_error("bad \\u escape", j) end
                local cp = tonumber(hex, 16)
                j = j + 6
                if cp >= 0xD800 and cp <= 0xDBFF and s:sub(j, j + 1) == "\\u" then
                    local lo = tonumber(s:sub(j + 2, j + 5), 16)
                    if lo and lo >= 0xDC00 and lo <= 0xDFFF then
                        cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00)
                        j = j + 6
                    end
                end
                if cp >= 0xD800 and cp <= 0xDFFF then cp = 0xFFFD end
                out[#out + 1] = utf8.char(cp)
            else
                decode_error("bad escape", j)
            end
        else
            local k = s:find('[\\"]', j) or (#s + 1)
            out[#out + 1] = s:sub(j, k - 1)
            j = k
        end
    end
end

decode_value = function(s, i)
    i = skip(s, i)
    local c = s:sub(i, i)
    if c == "{" then
        local obj = {}
        i = skip(s, i + 1)
        if s:sub(i, i) == "}" then return obj, i + 1 end
        while true do
            if s:sub(i, i) ~= '"' then decode_error("expected a key", i) end
            local key
            key, i = decode_string(s, i)
            i = skip(s, i)
            if s:sub(i, i) ~= ":" then decode_error("expected ':'", i) end
            obj[key], i = decode_value(s, i + 1)
            i = skip(s, i)
            local d = s:sub(i, i)
            if d == "}" then return obj, i + 1 end
            if d ~= "," then decode_error("expected ',' or '}'", i) end
            i = skip(s, i + 1)
        end
    elseif c == "[" then
        local arr, n = {}, 0
        i = skip(s, i + 1)
        if s:sub(i, i) == "]" then return arr, i + 1 end
        while true do
            n = n + 1
            arr[n], i = decode_value(s, i)
            i = skip(s, i)
            local d = s:sub(i, i)
            if d == "]" then return arr, i + 1 end
            if d ~= "," then decode_error("expected ',' or ']'", i) end
            i = i + 1
        end
    elseif c == '"' then
        return decode_string(s, i)
    elseif s:find("^true", i) then
        return true, i + 4
    elseif s:find("^false", i) then
        return false, i + 5
    elseif s:find("^null", i) then
        return nil, i + 4
    else
        local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", i)
        if not num or num == "" then decode_error("unexpected character", i) end
        return math.tointeger(tonumber(num)) or tonumber(num), i + #num
    end
end

function M.json_decode(s)
    local ok, value, pos = pcall(decode_value, s, 1)
    if not ok then
        if type(value) == "table" and value.json then return nil, value.msg end
        return nil, tostring(value)
    end
    if skip(s, pos) <= #s then return nil, "trailing characters at byte " .. pos end
    return value
end

-- Replaces every byte that is not part of a valid UTF-8 sequence with U+FFFD, so the answer is valid JSON.
function M.valid_utf8(s)
    -- Strict utf8.len rejects overlong forms, surrogates and values above U+10FFFF.
    if utf8.len(s) then return s end
    local out, i, n = {}, 1, #s
    while i <= n do
        local b = s:byte(i)
        local len = b < 0x80 and 1 or (b >= 0xC2 and b <= 0xDF) and 2 or (b >= 0xE0 and b <= 0xEF) and 3
            or (b >= 0xF0 and b <= 0xF4) and 4 or 0
        local ok = len > 0 and i + len - 1 <= n
        for k = 1, len - 1 do
            if not ok then break end
            local c = s:byte(i + k)
            ok = c >= 0x80 and c <= 0xBF
        end
        if ok and len > 1 then
            local c1 = s:byte(i + 1)
            if b == 0xE0 then ok = c1 >= 0xA0 end
            if b == 0xED then ok = c1 <= 0x9F end
            if b == 0xF0 then ok = c1 >= 0x90 end
            if b == 0xF4 then ok = c1 <= 0x8F end
        end
        if ok then
            out[#out + 1] = s:sub(i, i + len - 1)
            i = i + len
        else
            out[#out + 1] = "\239\191\189"
            i = i + 1
        end
    end
    return table.concat(out)
end

local function encode_string(s)
    s = M.valid_utf8(s)
    return '"' .. s:gsub('[%c"\\]', function(c)
        if c == '"' then return '\\"' end
        if c == "\\" then return "\\\\" end
        if c == "\n" then return "\\n" end
        if c == "\r" then return "\\r" end
        if c == "\t" then return "\\t" end
        return string.format("\\u%04x", c:byte())
    end) .. '"'
end

local function encode_strings(list)
    local parts = {}
    for i = 1, #list do parts[i] = encode_string(list[i]) end
    return "[" .. table.concat(parts, ",") .. "]"
end

function M.encode_answer(a)
    return string.format('{"protocol":%d,"id":%s,"ok":%s,"error":%s,"values":%s,"prints":%s,"mods":%s,"truncated":%s}',
        M.PROTOCOL, encode_string(a.id or ""), tostring(a.ok == true), encode_string(a.error or ""),
        encode_strings(a.values or {}), encode_strings(a.prints or {}), encode_strings(a.mods or {}), tostring(a.truncated == true))
end

function M.encode_heartbeat(bridge, version, mod, time)
    return string.format('{"protocol":%d,"bridge":%s,"version":%s,"mod":%s,"time":%d}', M.PROTOCOL, encode_string(bridge),
        encode_string(version), encode_string(mod), time)
end

---------------------------------------------------------------------------------------------------- values

local OBJECT_TYPES = { UObject = true, AActor = true, UClass = true, UFunction = true, UInterface = true, UDataTable = true,
    UWorld = true, UEnum = true, UScriptStruct = true }

local function call(f, ...)
    local ok, r = pcall(f, ...)
    if ok then return r end
end

-- Display string for one value, as UE4SS's native REPL writes it. Metamethods are not called for tables;
-- UE4SS objects are asked for their name through their own methods, inside pcall.
function M.show(v)
    local t = type(v)
    if t == "nil" or t == "boolean" or t == "number" then return tostring(v) end
    if t == "string" then return '"' .. v:sub(1, M.MAX_VALUE_LENGTH + 1) .. '"' end
    if t == "table" then
        local n, k, more = 0, nil, ""
        while true do
            k = next(v, k)
            if k == nil then break end
            n = n + 1
            if n >= M.MAX_TABLE_ENTRIES_COUNTED then
                more = "+"
                break
            end
        end
        return string.format("table: %p (%d%s entries)", v, n, more)
    end
    if t == "function" then
        local info = debug.getinfo(v, "S")
        if info.what == "C" then return string.format("function: builtin: %p", v) end
        return string.format("function: %s:%d", info.short_src, info.linedefined)
    end
    if t == "thread" then return string.format("thread: %p", v) end
    if t == "userdata" then
        local mt = debug.getmetatable(v)
        local name = mt and rawget(mt, "__name")
        name = type(name) == "string" and name or "userdata"
        if OBJECT_TYPES[name] then
            local valid = call(function() return v:IsValid() end)
            local full = valid and call(function() return v:GetFullName() end)
            if type(full) == "string" then return name .. ": " .. full end
            return name .. ": (invalid)"
        end
        if name == "FNameUserdata" then
            local text = call(function() return v:ToString() end)
            if type(text) == "string" then return 'FName: "' .. text .. '"' end
        end
        return string.format("%s: %p", name, v)
    end
    return t
end

local function clip(s, max, a)
    if #s > max then
        a.truncated = true
        return s:sub(1, max) .. "..."
    end
    return s
end

---------------------------------------------------------------------------------------------------- running a request

-- The limit's hook while a request runs. A hook set from Lua does not reach new coroutines (the debug library
-- looks the hook function up per thread), so the request's coroutine.create/wrap install it on each one.
local current_hook

local request_coroutine = setmetatable({
    create = function(f)
        local co = coroutine.create(f)
        if current_hook then debug.sethook(co, current_hook, "", 1000) end
        return co
    end,
}, { __index = coroutine })

request_coroutine.wrap = function(f)
    local co = request_coroutine.create(f)
    return function(...)
        local r = table.pack(coroutine.resume(co, ...))
        if r[1] then return table.unpack(r, 2, r.n) end
        coroutine.close(co)
        error(r[2], 0)
    end
end

-- The environment requests run in: the bridge's globals, plus a print that also goes to the answer.
function M.new_env(G, log_print)
    local env = setmetatable({ coroutine = request_coroutine }, { __index = G })
    local capture
    env.print = function(...)
        local n, parts = select("#", ...), {}
        for i = 1, n do parts[i] = tostring((select(i, ...))) end
        local text = table.concat(parts, "\t")
        if capture then capture(text) end
        if log_print then log_print(...) end
    end
    env.println = env.print
    return env, function(f) capture = f end
end

-- Runs fn under an instruction budget. Once spent, the hook raises before every instruction, on the current thread
-- and the request's thread, so a loop around pcall still stops.
local function limited(fn, limit_ms)
    local root = coroutine.running()
    local budget = limit_ms * M.INSTRUCTIONS_PER_MS
    local backstop = os.time() + math.ceil(limit_ms / 1000) + 1
    local used, active, spent = 0, true, false
    local message = string.format("the request ran longer than its limit of %d ms", limit_ms)
    -- Not a tail call, so this frame stays on the stack while any of the request's code runs on this thread.
    local function body()
        local r = table.pack(fn())
        return table.unpack(r, 1, r.n)
    end
    local function in_request()
        for level = 2, 250 do
            local info = debug.getinfo(level, "f")
            if not info then return false end
            if info.func == body then return true end
        end
        return true
    end
    local hook
    hook = function()
        local co = coroutine.running()
        if not active then
            debug.sethook(co, nil)
            return
        end
        if not spent then
            used = used + 1000
            if used < budget and os.time() <= backstop then return end
            spent = true
        end
        -- Raise only in the request's own code: once xpcall has returned, the bridge's code runs on this thread too.
        if co == root and not in_request() then return end
        debug.sethook(co, hook, "", 1)
        if co ~= root then debug.sethook(root, hook, "", 1) end
        error(message, 2)
    end
    current_hook = hook
    debug.sethook(root, hook, "", 1000)
    local results = table.pack(xpcall(body, debug.traceback))
    active, current_hook = false, nil
    debug.sethook(root, nil)
    return results
end

-- One request: text in, answer table out. `mod` is the bridge's own name, the only valid target.
function M.handle(text, env, set_capture, mod)
    local a = { id = "", ok = false, error = "", values = {}, prints = {}, mods = {}, truncated = false }
    local request, err = M.json_decode(text)
    if type(request) ~= "table" then
        a.error = "The request is not valid JSON: " .. tostring(err)
        return a
    end
    a.id = type(request.id) == "string" and request.id or ""
    if request.protocol ~= M.PROTOCOL then
        a.error = string.format("Unsupported protocol %s; this VibeBridge speaks protocol %d", tostring(request.protocol), M.PROTOCOL)
        return a
    end
    if request.op == "list" then
        a.ok, a.mods = true, { mod }
        return a
    end
    if request.op ~= "exec" then
        a.error = string.format("Unknown op '%s'; expected 'exec' or 'list'", tostring(request.op))
        return a
    end
    if type(request.mod) ~= "string" or request.mod == "" or type(request.code) ~= "string" or request.code == "" then
        a.error = "'exec' needs 'mod' and 'code'"
        return a
    end
    if request.mod ~= mod then
        a.error = string.format("VibeBridge can only run code in its own Lua state, '%s'; this UE4SS build has no external "
            .. "REPL that reaches '%s' (UE4SS PR #1475 adds one)", mod, request.mod)
        return a
    end
    local limit = request.timeout_ms == nil and M.DEFAULT_LIMIT_MS or request.timeout_ms
    if math.type(limit) ~= "integer" or limit < 1 or limit > M.MAX_LIMIT_MS then
        a.error = string.format("'timeout_ms' must be between 1 and %d", M.MAX_LIMIT_MS)
        return a
    end

    local fn, load_err = load("return " .. request.code, "=VibeBridge", "t", env)
    if not fn then fn, load_err = load(request.code, "=VibeBridge", "t", env) end
    if not fn then
        a.error = clip(load_err, M.MAX_ERROR_LENGTH, a)
        return a
    end

    local bytes = 0
    set_capture(function(line)
        if bytes >= M.MAX_PRINT_BYTES or #a.prints >= M.MAX_PRINTS then
            a.truncated = true
            return
        end
        if #line > M.MAX_PRINT_BYTES - bytes then
            line = line:sub(1, M.MAX_PRINT_BYTES - bytes)
            a.truncated = true
        end
        bytes = bytes + #line
        a.prints[#a.prints + 1] = line
    end)
    local results = limited(fn, limit)
    set_capture(nil)

    if not results[1] then
        a.error = clip(type(results[2]) == "string" and results[2] or ("(error object is a " .. type(results[2]) .. " value)"),
            M.MAX_ERROR_LENGTH, a)
        return a
    end
    for i = 2, results.n do
        if #a.values >= M.MAX_VALUES then
            a.truncated = true
            break
        end
        a.values[#a.values + 1] = clip(M.show(results[i]), M.MAX_VALUE_LENGTH, a)
    end
    a.ok = true
    return a
end

return M
