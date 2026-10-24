--[[ uvmod's call census: which functions run when something happens. It hooks every function of the classes you
     name (by default only Blueprint functions, the game's own code) and counts their calls in windows; comparing an
     idle window with one in which the player acted (or `input` pressed something) shows what the action ran. That
     is the step before `watch`, when you do not know yet which function to watch.

     Answer lines (tab-separated):
       H <hooked> <failed> <skipped>             hooks in place after start
       C <window> <seconds> <calls> <functions>  one closed window
       F <count> <per second> <path>             one function's calls in a window (most first)
       D <before/s> <after/s> <path>             compare: a function new in, or much busier in, the later window
       V <text> | W <text> ]]
do
    local VERSION = 1
    local old = rawget(_ENV, "__uvmod_census")
    if type(old) ~= "table" or old.version ~= VERSION then
        if type(old) == "table" and type(old.stop) == "function" then pcall(old.stop) end
        local C = { version = VERSION, hooks = {}, n = 0, counts = {}, windows = {}, started = nil, max_hooks = 2000 }

        local function try(f, ...)
            local ok, r = pcall(f, ...)
            if ok then return r end
            return nil
        end
        local function valid(o) return o ~= nil and try(function() return o:IsValid() end) == true end
        local function path_of(o)
            local full = try(function() return o:GetFullName() end) or ""
            return full:match("^%S+%s+(.+)$") or full
        end

        local function now()
            local pc = try(function() return FindFirstOf("PlayerController") end)
            local gs = try(function() return StaticFindObject("/Script/Engine.Default__GameplayStatics") end)
            if valid(pc) and valid(gs) then
                local t = try(function() return gs:GetRealTimeSeconds(pc) end)
                if type(t) == "number" then return t end
            end
            return os.clock()
        end

        local function resolve(spec)
            local inspect = rawget(_ENV, "__uvmod_inspect")
            if type(inspect) == "table" and inspect.resolve then
                local o, err = inspect.resolve(spec)
                if not o then error(err, 0) end
                return o
            end
            local o = spec:sub(1, 1) == "/" and try(function() return StaticFindObject(spec) end)
                or try(function() return FindFirstOf((spec:gsub("^first:", ""))) end)
            if not valid(o) then error("cannot resolve " .. spec, 0) end
            return o
        end

        local function class_chain(o)
            local cls = try(function() return o:type() end) == "UClass" and o or try(function() return o:GetClass() end)
            local chain, n = {}, 0
            while valid(cls) and n < 32 do
                chain[#chain + 1] = cls
                cls = try(function() return cls:GetSuperStruct() end)
                n = n + 1
            end
            return chain
        end

        -- which: "bp" (classes under /Game/, the game's Blueprints) or "all" (also native, but never CoreUObject).
        function C.start(specs, which, pattern)
            which = which or "bp"
            local p = pattern and pattern:lower() or nil
            local hooked, failed, skipped = 0, 0, 0
            for _, spec in ipairs(specs) do
                for _, cls in ipairs(class_chain(resolve(spec))) do
                    local cpath = path_of(cls)
                    local wanted = cpath:sub(1, 6) == "/Game/" or (which == "all" and not cpath:find("^/Script/CoreUObject%."))
                    if wanted then
                        local fns = {}
                        pcall(function() cls:ForEachFunction(function(f) fns[#fns + 1] = f end) end)
                        for _, f in ipairs(fns) do
                            local fpath = path_of(f)
                            if C.hooks[fpath] then
                                -- already counted
                            elseif p and not fpath:lower():find(p, 1, true) then
                                skipped = skipped + 1
                            elseif C.n >= C.max_hooks then
                                skipped = skipped + 1
                            else
                                local ok, pre, post = pcall(RegisterHook, fpath, function()
                                    local c = C.counts
                                    c[fpath] = (c[fpath] or 0) + 1
                                end)
                                if ok then
                                    C.hooks[fpath] = { pre, post }
                                    C.n = C.n + 1
                                    hooked = hooked + 1
                                else
                                    failed = failed + 1
                                end
                            end
                        end
                    end
                end
            end
            C.counts, C.started = {}, now()
            return string.format("H\t%d\t%d\t%d", C.n, failed, skipped)
        end

        local function window_lines(w, limit)
            local rows = {}
            for path, n in pairs(w.counts) do rows[#rows + 1] = { n, path } end
            table.sort(rows, function(a, b) return a[1] > b[1] or (a[1] == b[1] and a[2] < b[2]) end)
            local calls = 0
            for _, r in ipairs(rows) do calls = calls + r[1] end
            local out = { string.format("C\t%d\t%.2f\t%d\t%d", w.index, w.seconds, calls, #rows) }
            for i, r in ipairs(rows) do
                if i > (limit or 40) then break end
                out[#out + 1] = string.format("F\t%d\t%.1f\t%s", r[1], r[1] / math.max(w.seconds, 0.001), r[2])
            end
            return out
        end

        -- Close the current window (its counts are returned) and open the next one.
        function C.mark(limit)
            if not C.started then error("no census running: start one first", 0) end
            local t = now()
            local w = { index = #C.windows + 1, seconds = t - C.started, counts = C.counts }
            C.windows[#C.windows + 1] = w
            if #C.windows > 8 then table.remove(C.windows, 1) end
            C.counts, C.started = {}, t
            return table.concat(window_lines(w, limit), "\n")
        end

        -- The last window against the one before: functions new in it, or at least `factor` times busier.
        function C.compare(factor, limit)
            factor = factor or 3
            local n = #C.windows
            if n < 2 then error("compare needs two closed windows (mark twice)", 0) end
            local a, b = C.windows[n - 1], C.windows[n]
            local rows = {}
            for path, cnt in pairs(b.counts) do
                local after = cnt / math.max(b.seconds, 0.001)
                local before = (a.counts[path] or 0) / math.max(a.seconds, 0.001)
                if before == 0 or after >= before * factor then rows[#rows + 1] = { before, after, path, cnt } end
            end
            table.sort(rows, function(x, y)
                if (x[1] == 0) ~= (y[1] == 0) then return x[1] == 0 end
                return x[4] > y[4]
            end)
            local out = { string.format("V\twindow %d (%.1f s) against window %d (%.1f s)", b.index, b.seconds, a.index, a.seconds) }
            for i, r in ipairs(rows) do
                if i > (limit or 60) then break end
                out[#out + 1] = string.format("D\t%.1f\t%.1f\t%s", r[1], r[2], r[3])
            end
            if #rows == 0 then out[#out + 1] = "V\tnothing ran in the later window that did not run as often before" end
            return table.concat(out, "\n")
        end

        function C.stop()
            local n, failed = 0, 0
            for fpath, ids in pairs(C.hooks) do
                if pcall(UnregisterHook, fpath, ids[1], ids[2]) then n = n + 1 else failed = failed + 1 end
            end
            C.hooks, C.n, C.counts, C.windows, C.started = {}, 0, {}, {}, nil
            return string.format("V\tremoved %d hooks%s", n, failed > 0 and (", " .. failed .. " failed") or "")
        end

        __uvmod_census = C
    end
end
