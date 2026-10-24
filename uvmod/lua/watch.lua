--[[ uvmod's watch helper. It records calls of hooked UFunctions into a bounded buffer in this Lua state, for
     `uvmod watch`, `events` and `wait`.

     uvmod sends this file ahead of every watch request. It defines the global __uvmod once per Lua state (again
     only when VERSION changes) and does nothing on later runs, so the buffer and the watches survive between
     requests. Results go back as return values, never through print, which also writes to UE4SS.log.

     Answer lines, tab-separated (text fields have no tabs or line breaks):
       C <cursor> <lost> <pending>                              the events below end at <cursor>
       L <id,id,...>                                            the watches in place
       E <seq> <clock> <watch> <text>                           one recorded call
       W <id> <pre|post> <calls> <recorded> <limit> <errors> <function> <last error> ]]
do
    local VERSION = 2
    local old = rawget(_ENV, "__uvmod")
    if type(old) ~= "table" or old.version ~= VERSION then
        if type(old) == "table" and type(old.unwatch_all) == "function" then pcall(old.unwatch_all) end

        local ENV = _ENV
        local U = { version = VERSION, seq = 0, read = 0, max = 1000, events = {}, watches = {}, order = {} }
        local MAX_TEXT, VALUE_BYTES = 240, 3800
        local OBJECT_TYPES = { UObject = true, AActor = true, UClass = true, UFunction = true, UInterface = true,
            UDataTable = true, UWorld = true, UEnum = true, UScriptStruct = true }

        local function try(f)
            local ok, r = pcall(f)
            if ok then return r end
        end

        -- One line of text: control characters become spaces, and it is cut to MAX_TEXT bytes at a character edge.
        local function clean(s)
            s = tostring(s):gsub("%c", " ")
            if #s > MAX_TEXT then
                local cut = MAX_TEXT
                while cut > 0 and (s:byte(cut + 1) or 0) >= 0x80 and (s:byte(cut + 1) or 0) < 0xC0 do cut = cut - 1 end
                s = s:sub(1, cut) .. "..."
            end
            return s
        end

        local function object_name(v)
            if try(function() return v:IsValid() end) == false then return "(invalid)" end
            local s = try(function() return v:GetFName():ToString() end)
            if type(s) == "string" then return s end
            s = try(function() return v:GetFullName() end)
            if type(s) == "string" then return s end
        end

        -- A short display string, taken when the call happens (the object may be gone by the time anyone reads it).
        local fmt
        fmt = function(v, depth)
            local t = type(v)
            if t == "string" then return depth > 0 and ('"' .. v .. '"') or v end
            if t == "number" then return math.type(v) == "float" and string.format("%.6g", v) or tostring(v) end
            if t ~= "table" and t ~= "userdata" then return tostring(v) end
            local mt = debug.getmetatable(v)
            local name = mt and rawget(mt, "__name")
            if OBJECT_TYPES[name] then return object_name(v) or name end
            if t == "userdata" then
                local s = try(function() return v:ToString() end)
                if type(s) == "string" then return s end
                return type(name) == "string" and name or "userdata"
            end
            if depth >= 2 then return "{...}" end
            local keys, more = {}, false
            for k in next, v do
                if #keys == 8 then
                    more = true
                    break
                end
                keys[#keys + 1] = k
            end
            table.sort(keys, function(a, b)
                local ta, tb = type(a), type(b)
                if ta ~= tb then return ta < tb end
                if ta == "number" or ta == "string" then return a < b end
                return tostring(a) < tostring(b)
            end)
            local parts = {}
            for i, k in ipairs(keys) do
                local text = fmt(rawget(v, k), depth + 1)
                parts[i] = k == i and text or (tostring(k) .. "=" .. text)
            end
            return "{" .. table.concat(parts, ", ") .. (more and ", ..." or "") .. "}"
        end

        -- Answer lines as few string values as fit (the REPL returns at most 64 values of 4 KiB).
        local function pack(lines)
            local values, chunk, size = {}, {}, 0
            for _, line in ipairs(lines) do
                if size + #line + 1 > VALUE_BYTES and #chunk > 0 then
                    values[#values + 1] = table.concat(chunk, "\n")
                    chunk, size = {}, 0
                end
                chunk[#chunk + 1] = line
                size = size + #line + 1
            end
            if #chunk > 0 then values[#values + 1] = table.concat(chunk, "\n") end
            return table.unpack(values)
        end

        local function push(id, text)
            local seq = U.seq + 1
            U.seq = seq
            U.events[seq] = { seq, os.clock(), id, text }
            U.events[seq - U.max] = nil
        end

        local function watch_line(w)
            return string.format("W\t%s\t%s\t%d\t%d\t%d\t%d\t%s\t%s", w.id, w.when, w.hits, w.recorded, w.limit, w.errors,
                clean(w.path), clean(w.last_error or ""))
        end

        local function list_line()
            return "L\t" .. table.concat(U.order, ",")
        end

        function U.watch(id, path, capture, when, limit)
            if type(id) ~= "string" or not id:match("^[%w_.-]+$") then error("a watch id is letters, digits, _ . -", 0) end
            if type(path) ~= "string" or path == "" then error("watch needs a function path", 0) end
            local cap
            if capture ~= nil and capture ~= "" then
                local chunk, err = load("return function(self, ...) return " .. capture .. "\nend", "=capture", "t", ENV)
                if not chunk then error("the capture does not compile: " .. tostring(err), 0) end
                cap = chunk()
            end
            if U.watches[id] then U.unwatch(id) end
            local w = { id = id, path = path, when = when == "post" and "post" or "pre", limit = limit or 200, hits = 0,
                recorded = 0, errors = 0 }

            local function record(ctx, ...)
                if w.recorded >= w.limit then return end
                local self_obj = try(function() return ctx:get() end)
                local text
                if cap then
                    local n, args = select("#", ...), { ... }
                    for i = 1, n do
                        local p = args[i]
                        local ok, v = pcall(function() return p:get() end)
                        if ok then args[i] = v end
                    end
                    local r = table.pack(pcall(cap, self_obj, table.unpack(args, 1, n)))
                    if not r[1] then
                        w.errors = w.errors + 1
                        w.last_error = tostring(r[2])
                        if w.errors > 3 then return end
                        text = "capture error: " .. tostring(r[2])
                    elseif r.n == 1 or (r.n == 2 and r[2] == nil) then
                        return -- the capture skips this call
                    else
                        local parts = {}
                        for i = 2, r.n do parts[#parts + 1] = fmt(r[i], 0) end
                        text = table.concat(parts, ", ")
                    end
                else
                    text = self_obj ~= nil and ("self=" .. fmt(self_obj, 0)) or "called"
                end
                w.recorded = w.recorded + 1
                push(id, clean(text))
            end

            -- Runs inside the game on every call: never let an error escape into UE4SS's hook dispatch.
            local function on_call(...)
                w.hits = w.hits + 1
                local ok, err = pcall(record, ...)
                if not ok then
                    w.errors = w.errors + 1
                    w.last_error = tostring(err)
                end
            end

            local ok, pre, post
            if w.when == "post" then
                ok, pre, post = pcall(RegisterHook, path, function() end, on_call)
            else
                ok, pre, post = pcall(RegisterHook, path, on_call)
            end
            if not ok then error("RegisterHook failed: " .. tostring(pre), 0) end
            if w.when == "post" and pre == post then
                -- UE4SS keeps only the pre callback of a Blueprint function's hook, and says so by giving it one id.
                pcall(UnregisterHook, path, pre, post)
                error("UE4SS does not run post callbacks for Blueprint functions, and this is one; watch it without post", 0)
            end
            w.pre, w.post = pre, post
            U.watches[id] = w
            U.order[#U.order + 1] = id
            return pack({ list_line(), watch_line(w) })
        end

        function U.unwatch(id)
            local w = U.watches[id]
            if not w then return pack({ list_line() }) end
            w.limit = -1 -- stops recording even if unregistering fails
            U.watches[id] = nil
            for i, other in ipairs(U.order) do
                if other == id then
                    table.remove(U.order, i)
                    break
                end
            end
            local ok, err = pcall(UnregisterHook, w.path, w.pre, w.post)
            if not ok then error("UnregisterHook failed: " .. tostring(err), 0) end
            return pack({ list_line() })
        end

        function U.unwatch_all()
            local ids = { table.unpack(U.order) }
            local failed = {}
            for _, id in ipairs(ids) do
                local ok, err = pcall(U.unwatch, id)
                if not ok then failed[#failed + 1] = id .. ": " .. tostring(err) end
            end
            if #failed > 0 then error(table.concat(failed, "; "), 0) end
            return pack({ list_line() })
        end

        function U.list()
            local lines = { list_line() }
            for _, id in ipairs(U.order) do lines[#lines + 1] = watch_line(U.watches[id]) end
            return pack(lines)
        end

        -- Events after `since` (default: after the last drain), at most `limit`.
        function U.drain(since, limit)
            since = since or U.read
            if since > U.seq then since = 0 end -- the buffer is younger than the caller's cursor
            limit = limit or 200
            local first = math.max(since + 1, U.seq - U.max + 1)
            local last = math.min(U.seq, first + limit - 1)
            local cursor = math.max(last, first - 1)
            local lines = { string.format("C\t%d\t%d\t%d", cursor, first - since - 1, U.seq - cursor), list_line() }
            for i = first, last do
                local e = U.events[i]
                lines[#lines + 1] = string.format("E\t%d\t%.3f\t%s\t%s", e[1], e[2], e[3], e[4])
            end
            for _, id in ipairs(U.order) do
                local w = U.watches[id]
                if w.recorded >= w.limit or w.errors > 0 then lines[#lines + 1] = watch_line(w) end
            end
            U.read = cursor
            return pack(lines)
        end

        __uvmod = U
    end
end
