--[[ uvmod's input helper: play the game through its own Enhanced Input, so an agent can test a mod's controls with
     nobody at the keyboard. It injects a value for an InputAction through the local player's
     EnhancedInputLocalPlayerSubsystem, once or on every frame for a while (a held key, a stick held over, a mouse
     moved for some time). The game's own mapping contexts, triggers and modifiers run as for real input.

     InjectInputForAction takes an FInputActionValue, a struct with no reflected fields (UE4SS cannot fill it), so this
     uses InjectInputVectorForAction: X carries a button or 1D axis, X/Y a 2D axis (move, look), X/Y/Z a 3D one.
     Measured on RV There Yet? (UE 5.6): the action's input events fire as for a key press.

     Answer lines (tab-separated): A <name> <value type> <path> | V <text> | W <warning> ]]
do
    local VERSION = 1
    local old = rawget(_ENV, "__uvmod_input")
    if type(old) ~= "table" or old.version ~= VERSION then
        if type(old) == "table" and type(old.stop) == "function" then pcall(old.stop) end
        local I = { version = VERSION, holds = {} }
        local INTERFACE = "/Script/EnhancedInput.EnhancedInputSubsystemInterface:InjectInputVectorForAction"

        local function try(f, ...)
            local ok, r = pcall(f, ...)
            if ok then return r end
            return nil
        end
        local function valid(o) return o ~= nil and try(function() return o:IsValid() end) == true end
        local function fname(o) return try(function() return o:GetFName():ToString() end) end

        local function subsystem()
            local s = try(function() return FindFirstOf("EnhancedInputLocalPlayerSubsystem") end)
            if not valid(s) then error("no EnhancedInputLocalPlayerSubsystem: this game does not use Enhanced Input, or no local player exists yet", 0) end
            return s
        end

        local function inject_fn()
            local f = try(function() return StaticFindObject(INTERFACE) end)
            if not valid(f) then error("InjectInputVectorForAction not found (Enhanced Input plugin missing or too old)", 0) end
            return f
        end

        local function action(spec)
            if type(spec) ~= "string" or spec == "" then error("name an InputAction (e.g. IA_Jump or its full path)", 0) end
            if spec:sub(1, 1) == "/" then
                local a = try(function() return StaticFindObject(spec) end)
                if valid(a) then return a end
                error("no InputAction at " .. spec, 0)
            end
            local want = spec:lower()
            for _, a in ipairs(try(function() return FindAllOf("InputAction") end) or {}) do
                if valid(a) and (fname(a) or ""):lower() == want then return a end
            end
            error("no loaded InputAction named " .. spec .. " (list them with: input list)", 0)
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

        local function lines_out(lines) return table.concat(lines, "\n") end

        function I.list(pattern)
            local p = (pattern or ""):lower()
            local rows = {}
            for _, a in ipairs(try(function() return FindAllOf("InputAction") end) or {}) do
                local name = fname(a)
                if name and (p == "" or name:lower():find(p, 1, true)) then
                    local vt = try(function() return a.ValueType end)
                    local names = { [0] = "button", [1] = "axis1d", [2] = "axis2d", [3] = "axis3d" }
                    local path = try(function() return a:GetFullName():match("^%S+%s+(.+)$") end) or "?"
                    rows[#rows + 1] = string.format("A\t%s\t%s\t%s", name, names[vt] or tostring(vt), path)
                end
            end
            table.sort(rows)
            if #rows == 0 then rows[1] = "W\tno loaded InputAction matches" end
            return lines_out(rows)
        end

        local function inject(sub, fn, a, x, y, z)
            fn(sub, a, { X = x, Y = y, Z = z }, {}, {})
        end

        -- Inject (x, y, z) for the action now and, with hold_ms > 0, on every frame until hold_ms have passed.
        function I.press(spec, x, y, z, hold_ms)
            local sub, fn, a = subsystem(), inject_fn(), action(spec)
            x, y, z, hold_ms = x or 1.0, y or 0.0, z or 0.0, hold_ms or 0
            local name = fname(a)
            I.release(name)
            inject(sub, fn, a, x, y, z)
            if hold_ms <= 0 then
                return lines_out({ string.format("V\tinjected %s = (%g, %g, %g) for one frame", name, x, y, z) })
            end
            local hold = { name = name, stop_at = now() + hold_ms / 1000, frames = 1 }
            local function tick()
                if hold.done then return true end
                if not valid(a) or not valid(sub) or now() >= hold.stop_at then
                    hold.done = true
                    I.holds[name] = nil
                    return true
                end
                local ok = pcall(inject, sub, fn, a, x, y, z)
                hold.frames = hold.frames + 1
                if not ok then hold.done = true I.holds[name] = nil return true end
                return false
            end
            if type(LoopInGameThreadAfterFrames) == "function" and EngineTickAvailable ~= false then
                hold.handle = LoopInGameThreadAfterFrames(1, function()
                    if tick() and hold.handle then pcall(CancelDelayedAction, hold.handle) end
                end)
                hold.via = "every frame"
            else
                -- Older UE4SS: an async loop that hands each injection to the game thread.
                LoopAsync(10, function()
                    if hold.done then return true end
                    ExecuteInGameThread(tick)
                    return false
                end)
                hold.via = "every 10 ms"
            end
            I.holds[name] = hold
            return lines_out({ string.format("V\tholding %s = (%g, %g, %g) for %d ms (%s)", name, x, y, z, hold_ms, hold.via) })
        end

        function I.release(name)
            local h = name and I.holds[name]
            if h then
                h.done = true
                if h.handle then pcall(CancelDelayedAction, h.handle) end
                I.holds[name] = nil
            end
            return lines_out({ "V\treleased " .. tostring(name) })
        end

        function I.stop()
            local n = 0
            for name in pairs(I.holds) do
                I.release(name)
                n = n + 1
            end
            return lines_out({ "V\treleased " .. n .. " held action" .. (n == 1 and "" or "s") })
        end

        function I.status()
            local rows = {}
            for name, h in pairs(I.holds) do rows[#rows + 1] = string.format("V\t%s held, %d frames so far", name, h.frames) end
            if #rows == 0 then rows[1] = "V\tnothing held" end
            return lines_out(rows)
        end

        __uvmod_input = I
    end
end
