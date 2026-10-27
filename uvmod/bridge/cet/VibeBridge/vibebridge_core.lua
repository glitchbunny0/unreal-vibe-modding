--[[ VibeBridge for Cyber Engine Tweaks: the LuaRepl protocol (docs/protocol.md) in plain Lua that runs on LuaJIT (CET's
     Lua, 5.1 semantics) and on Lua 5.4 (the tests). init.lua wires it to CET (folder polling in onUpdate).

     What CET's sandbox does not give a mod, and what that means here:
     - no debug library: a request cannot be stopped after timeout_ms (the field is checked, not enforced), and errors
       come without a traceback;
     - no load(code, name, mode, env): code is compiled with CET's loadstring, which puts it in the mod's own
       environment, so globals a request sets persist for the next one;
     - no utf8 library and no table.pack, math.type or %p. ]]

local M = {}

M.PROTOCOL = 1
M.MAX_LIMIT_MS = 5000
M.DEFAULT_LIMIT_MS = 250
M.MAX_VALUES, M.MAX_VALUE_LENGTH = 64, 4096
M.MAX_PRINT_BYTES, M.MAX_PRINTS, M.MAX_ERROR_LENGTH = 64 * 1024, 10000, 16 * 1024
M.MAX_TABLE_ENTRIES_COUNTED = 100
M.CHUNK = 4000 -- bytes per value when a long text is returned in pieces (values hold up to 4096)

local unpack = table.unpack or unpack
local function pack(...) return { n = select("#", ...), ... } end

---------------------------------------------------------------------------------------------------- JSON

local function decode_error(msg, pos) error({ json = true, msg = msg .. " at byte " .. pos }, 0) end

local escapes = { ['"'] = '"', ["\\"] = "\\", ["/"] = "/", b = "\b", f = "\f", n = "\n", r = "\r", t = "\t" }

local function utf8_char(cp)
    if cp < 0x80 then return string.char(cp) end
    if cp < 0x800 then return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40) end
    if cp < 0x10000 then
        return string.char(0xE0 + math.floor(cp / 0x1000), 0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
    end
    return string.char(0xF0 + math.floor(cp / 0x40000), 0x80 + math.floor(cp / 0x1000) % 0x40,
        0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
end

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
                out[#out + 1] = utf8_char(cp)
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
        return tonumber(num), i + #num
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
    if not s:find("[\128-\255]") then return s end
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
M.encode_string = encode_string

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

-- The heartbeat, plus what this bridge runs in (`extra`: string fields such as the CET version).
function M.encode_heartbeat(bridge, version, mod, time, extra)
    local more = {}
    for k, v in pairs(extra or {}) do more[#more + 1] = "," .. encode_string(k) .. ":" .. encode_string(tostring(v)) end
    table.sort(more)
    return string.format('{"protocol":%d,"bridge":%s,"version":%s,"mod":%s,"time":%d%s}', M.PROTOCOL, encode_string(bridge),
        encode_string(version), encode_string(mod), math.floor(time), table.concat(more))
end

---------------------------------------------------------------------------------------------------- values

local function call(f, ...)
    local ok, r = pcall(f, ...)
    if ok then return r end
end

-- LuaJIT shows 64-bit integers (cdata) as 123ULL / -5LL; the digits are what a reader wants.
local function int64_digits(v)
    local text = call(tostring, v)
    return type(text) == "string" and text:match("^(%-?%d+)U?LL$") or nil
end

-- The class of a game object (CET's handle to an IScriptable), or nil for anything else.
local function class_name(v)
    local cn = call(function() return v:GetClassName() end)
    if cn == nil then return nil end
    local text = call(function() return Game.NameToString(cn) end)
    if type(text) == "string" and text ~= "" then return text end
    text = call(tostring, cn)
    return type(text) == "string" and (text:match("%-%-%[%[ (.-) %-%-%]%]") or text) or nil
end

-- Display string for one value. Metamethods are not called for tables; CET's own types (Vector4, CName, TweakDBID,
-- ItemID, enums) have a __tostring that reads like a Lua constructor; game objects show their class.
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
        return string.format("%s (%d%s entries)", tostring(v), n, more)
    end
    if t == "cdata" then return int64_digits(v) or tostring(v) end
    if t == "userdata" then
        local cls = class_name(v)
        if cls then return "object: " .. cls end
        local text = call(tostring, v)
        return type(text) == "string" and text or "userdata"
    end
    return tostring(v)
end

local function json_number(v)
    if v ~= v then return '"NaN"' end
    if v == math.huge then return '"Infinity"' end
    if v == -math.huge then return '"-Infinity"' end
    if v == math.floor(v) and v > -1e15 and v < 1e15 then return string.format("%d", math.floor(v)) end
    return string.format("%.14g", v)
end

-- JSON for any value: tables become arrays or objects, 64-bit integers numbers, game objects {"class": ...} with
-- `object_json(v)` (if given) deciding first; CET's own types go to `userdata_json(v)` (JSON text, if given) or their
-- display string. Cycles and depth beyond `max_depth` (default 12) become strings.
function M.json(value, userdata_json, max_depth, object_json)
    max_depth = max_depth or 12
    local seen = {}
    local function enc(v, depth)
        local t = type(v)
        if t == "nil" then return "null" end
        if t == "boolean" then return tostring(v) end
        if t == "number" then return json_number(v) end
        if t == "string" then return encode_string(v) end
        if t == "cdata" then
            local digits = int64_digits(v)
            return digits or encode_string(tostring(v))
        end
        if t == "userdata" then
            local cls = class_name(v)
            if cls then
                local custom = object_json and call(object_json, v, cls)
                if type(custom) == "string" then return custom end
                return '{"class":' .. encode_string(cls) .. "}"
            end
            local text = userdata_json and call(userdata_json, v)
            if type(text) == "string" and text ~= "" then return text end
            return encode_string(M.show(v))
        end
        if t ~= "table" then return encode_string(tostring(v)) end
        if seen[v] then return '"[cycle]"' end
        if depth >= max_depth then return '"[deeper than ' .. max_depth .. ' levels]"' end
        seen[v] = true
        local n, count = #v, 0
        for _ in pairs(v) do count = count + 1 end
        local parts = {}
        if n > 0 and count == n then
            for i = 1, n do parts[i] = enc(v[i], depth + 1) end
            seen[v] = nil
            return "[" .. table.concat(parts, ",") .. "]"
        end
        local keys = {}
        for k in pairs(v) do keys[#keys + 1] = k end
        table.sort(keys, function(a, b) return tostring(a) < tostring(b) end)
        for _, k in ipairs(keys) do
            parts[#parts + 1] = encode_string(tostring(k)) .. ":" .. enc(v[k], depth + 1)
        end
        seen[v] = nil
        return "{" .. table.concat(parts, ",") .. "}"
    end
    return enc(value, 0)
end

-- A long text as several return values of at most M.CHUNK bytes each, never splitting a UTF-8 character; the
-- client joins the values back together. Up to M.MAX_VALUES pieces.
function M.chunks(text)
    local out, i, n = {}, 1, #text
    while i <= n and #out < M.MAX_VALUES do
        local j = math.min(i + M.CHUNK - 1, n)
        while j < n and j > i do
            local b = text:byte(j + 1)
            if b < 0x80 or b >= 0xC0 then break end -- the next piece starts on a character
            j = j - 1
        end
        out[#out + 1] = text:sub(i, j)
        i = j + 1
    end
    if i <= n then out[#out] = out[#out] .. "\n[cut: more than " .. (M.MAX_VALUES * M.CHUNK) .. " bytes]" end
    return unpack(out)
end

local function clip(s, max, a)
    if #s > max then
        a.truncated = true
        return s:sub(1, max) .. "..."
    end
    return s
end

---------------------------------------------------------------------------------------------------- running a request

--[[ One request: text in, answer table out. `host` carries what CET provides:
     host.mod          the bridge's own name, the only valid target
     host.compile      function(code, chunkname) -> fn | nil, err   (CET: loadstring, into the mod's environment)
     host.set_print    function(f): make `print` in that environment f (nil: the original one)
     host.traceback    optional message handler for xpcall (absent in CET) ]]
function M.handle(text, host)
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
        a.ok, a.mods = true, { host.mod }
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
    if request.mod ~= host.mod then
        a.error = string.format("VibeBridge for CET runs code in its own environment, '%s', not in '%s' (CET gives each mod "
            .. "its own; reach another mod through GetMod(name))", host.mod, request.mod)
        return a
    end
    local limit = request.timeout_ms == nil and M.DEFAULT_LIMIT_MS or request.timeout_ms
    if type(limit) ~= "number" or limit ~= math.floor(limit) or limit < 1 or limit > M.MAX_LIMIT_MS then
        a.error = string.format("'timeout_ms' must be between 1 and %d", M.MAX_LIMIT_MS)
        return a
    end

    local fn, load_err = host.compile("return " .. request.code, "=VibeBridge")
    if not fn then fn, load_err = host.compile(request.code, "=VibeBridge") end
    if not fn then
        a.error = clip(tostring(load_err), M.MAX_ERROR_LENGTH, a)
        return a
    end

    local bytes = 0
    host.set_print(function(...)
        local n, parts = select("#", ...), {}
        for i = 1, n do parts[i] = tostring((select(i, ...))) end
        local line = table.concat(parts, "\t")
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
    local handler = host.traceback or function(e) return e end
    local results = pack(xpcall(fn, handler))
    host.set_print(nil)

    if not results[1] then
        local e = results[2]
        a.error = clip(type(e) == "string" and e or ("(error object is a " .. type(e) .. " value: " .. M.show(e) .. ")"),
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
