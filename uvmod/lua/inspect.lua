--[[ uvmod's inspect helper: read the game's reflection (find objects, list properties and functions, read a
     function's signature, list classes), call a UFunction behind a guard against the arguments that crash UE4SS
     natively, keep snapshots of an object and diff them, and trace from the camera to see what the player looks at.

     uvmod sends this file when a Lua state lacks it (or has an older VERSION). It defines the global
     __uvmod_inspect once and does nothing on later runs, so snapshots survive between requests. Answers are return
     values, never print, which also writes to UE4SS.log.

     Object references (`ref`), as strings:
       /Script/Engine.Default__KismetSystemLibrary   a full object path
       first:PlayerController                        the first live instance of a class (by short name)
       cdo:/Script/Engine.Character                  a class's default object
       class:/Script/Engine.Character                the class itself
       pc | pawn | world                             the local player controller, its pawn, its world

     Answer lines, tab-separated (text fields have no tabs or line breaks):
       O <full name>                                     the object the answer is about
       N <total> <shown>                                 counts, when a list was cut
       X <valid> <full name>                             one object found
       P <owner class> <name> <type> <value>             one property
       F <owner class> <name> <flags> <signature>        one function
       A <index> <name> <type> <role> <property class>   one parameter (role: in | ret | out?)
       K <name> <super> <path>                           one class
       V <text>                                          one value (a call's result, a target field)
       D <name> <before> <after>                         one changed property ((new) / (gone) for added / removed)
       S <label> <taken> <count> <ref>                   one snapshot
       W <text>                                          a warning ]]
do
    local VERSION = 1
    local old = rawget(_ENV, "__uvmod_inspect")
    if type(old) ~= "table" or old.version ~= VERSION then
        local ENV = _ENV
        local I = { version = VERSION, snaps = type(old) == "table" and type(old.snaps) == "table" and old.snaps or {} }
        local MAX_TEXT, VALUE_BYTES = 300, 3800

        -- Property classes whose values are safe and cheap to read. An allowlist, not a blocklist: reading a
        -- SoftObjectProperty has hard-crashed a game inside UE4SS's own reader.
        local READABLE = {
            BoolProperty = true, ByteProperty = true, Int8Property = true, Int16Property = true, IntProperty = true,
            Int64Property = true, UInt16Property = true, UInt32Property = true, UInt64Property = true,
            FloatProperty = true, DoubleProperty = true, NameProperty = true, StrProperty = true, TextProperty = true,
            EnumProperty = true, ObjectProperty = true, ClassProperty = true, StructProperty = true,
            ArrayProperty = true,
        }
        local SHORT = {
            BoolProperty = "bool", ByteProperty = "uint8", Int8Property = "int8", Int16Property = "int16",
            IntProperty = "int32", Int64Property = "int64", UInt16Property = "uint16", UInt32Property = "uint32",
            UInt64Property = "uint64", FloatProperty = "float", DoubleProperty = "double", NameProperty = "FName",
            StrProperty = "FString", TextProperty = "FText",
        }
        local OBJECT_PROPS = { ObjectProperty = true, ClassProperty = true, WeakObjectProperty = true,
            SoftObjectProperty = true, SoftClassProperty = true, LazyObjectProperty = true, InterfaceProperty = true }
        -- Blueprint functions list their locals after their parameters; the compiler's own locals start like this.
        local LOCAL_PREFIXES = { "CallFunc_", "K2Node_", "Temp_", "LocalVar" }
        local FUNC_FLAGS = { { 0x400, "native" }, { 0x2000, "static" }, { 0x04000000, "callable" },
            { 0x08000000, "event" }, { 0x10000000, "pure" }, { 0x40000000, "const" }, { 0x40, "net" },
            { 0x00200000, "server" }, { 0x01000000, "client" }, { 0x4000, "multicast" } }

        local function try(f, ...)
            local ok, r = pcall(f, ...)
            if ok then return r end
            return nil
        end

        local function clean(s)
            s = tostring(s):gsub("[%c]", " ")
            if #s > MAX_TEXT then
                local cut = MAX_TEXT
                while cut > 0 and (s:byte(cut + 1) or 0) >= 0x80 and (s:byte(cut + 1) or 0) < 0xC0 do cut = cut - 1 end
                s = s:sub(1, cut) .. "..."
            end
            return s
        end

        local function line(...)
            local parts = table.pack(...)
            for i = 1, parts.n do parts[i] = clean(parts[i] == nil and "" or parts[i]) end
            return table.concat(parts, "\t", 1, parts.n)
        end

        -- As few string values as fit (the REPL returns at most 64 values of 4 KiB).
        local function pack(lines)
            local values, chunk, size = {}, {}, 0
            for _, l in ipairs(lines) do
                if size + #l + 1 > VALUE_BYTES and #chunk > 0 then
                    values[#values + 1] = table.concat(chunk, "\n")
                    chunk, size = {}, 0
                end
                chunk[#chunk + 1] = l
                size = size + #l + 1
            end
            if #chunk > 0 then values[#values + 1] = table.concat(chunk, "\n") end
            if #values > 64 then
                values = { table.unpack(values, 1, 63) }
                values[64] = "W\tanswer cut: too many lines (narrow it with a pattern or a lower limit)"
            end
            return table.unpack(values)
        end

        local function utype(v) return try(function() return v:type() end) end
        local function is_valid(o) return try(function() return o:IsValid() end) == true end
        local function fname(o) return try(function() return o:GetFName():ToString() end) end
        local function fullname(o)
            if not is_valid(o) then return "(invalid)" end
            return try(function() return o:GetFullName() end) or fname(o) or "?"
        end
        local function is_fname(v) return utype(v) == "FName" end

        local function matcher(pattern)
            if pattern == nil or pattern == "" then return function() return true end end
            local p = pattern:lower()
            return function(s) return s ~= nil and s:lower():find(p, 1, true) ~= nil end
        end

        local function num(n)
            if math.type(n) == "integer" then return tostring(n) end
            if n == math.floor(n) and math.abs(n) < 1e15 then return string.format("%.1f", n) end
            return string.format("%.6g", n)
        end

        -- A short display string for any value UE4SS hands to Lua.
        local show
        show = function(v, depth)
            depth = depth or 2
            local t = type(v)
            if t == "nil" or t == "boolean" then return tostring(v) end
            if t == "number" then return num(v) end
            if t == "string" then return '"' .. v .. '"' end
            if t == "function" then return "function" end
            -- A plain table (UE4SS hands structs back as tables); anything answering :type() is a UE4SS object.
            local ut = utype(v)
            if t == "table" and type(ut) ~= "string" then
                local x, y, z = rawget(v, "X"), rawget(v, "Y"), rawget(v, "Z")
                if type(x) == "number" and type(y) == "number" and type(z) == "number" then
                    return string.format("(X=%s Y=%s Z=%s)", num(x), num(y), num(z))
                end
                local p, yw, r = rawget(v, "Pitch"), rawget(v, "Yaw"), rawget(v, "Roll")
                if type(p) == "number" and type(yw) == "number" and type(r) == "number" then
                    return string.format("(P=%s Y=%s R=%s)", num(p), num(yw), num(r))
                end
                if depth <= 0 then return "{...}" end
                local keys, more = {}, false
                for k in next, v do
                    if #keys == 12 then more = true break end
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
                    local text = show(rawget(v, k), depth - 1)
                    parts[i] = k == i and text or (tostring(k) .. "=" .. text)
                end
                return "{" .. table.concat(parts, ", ") .. (more and ", ..." or "") .. "}"
            end
            ut = type(ut) == "string" and ut or "userdata"
            if ut == "RemoteUnrealParam" or ut == "LocalUnrealParam" then
                local inner = try(function() return v:get() end)
                if inner ~= nil then return show(inner, depth) end
            end
            if ut == "FName" then return 'FName("' .. (try(function() return v:ToString() end) or "?") .. '")' end
            if ut == "FString" or ut == "FText" or ut == "FUtf8String" or ut == "FAnsiString" then
                return '"' .. (try(function() return v:ToString() end) or "?") .. '"'
            end
            if ut == "TArray" then
                local n = try(function() return v:GetArrayNum() end)
                if type(n) ~= "number" then return "TArray" end
                if depth <= 0 or n == 0 then return "TArray n=" .. n end
                local parts = {}
                for i = 1, math.min(n, 8) do parts[i] = show(try(function() return v[i] end), depth - 1) end
                return "TArray n=" .. n .. " [" .. table.concat(parts, ", ") .. (n > 8 and ", ..." or "") .. "]"
            end
            -- A struct: read the fields common structs have, before asking for a name (a struct wrapper answers
            -- GetFullName with its struct TYPE).
            local function field(k) return try(function() return v[k] end) end
            local tr = field("Translation")
            if tr ~= nil and field("Scale3D") ~= nil then
                return "(T=" .. show(tr, 0) .. " R=" .. show(field("Rotation"), 0) .. " S=" .. show(field("Scale3D"), 0) .. ")"
            end
            local x, y, z, w = field("X"), field("Y"), field("Z"), field("W")
            if type(x) == "number" and type(y) == "number" and type(z) == "number" and type(w) == "number" then
                return string.format("(X=%s Y=%s Z=%s W=%s)", num(x), num(y), num(z), num(w))
            end
            if type(x) == "number" and type(y) == "number" and type(z) == "number" then
                return string.format("(X=%s Y=%s Z=%s)", num(x), num(y), num(z))
            end
            if type(x) == "number" and type(y) == "number" then return string.format("(X=%s Y=%s)", num(x), num(y)) end
            local p, yw, r = field("Pitch"), field("Yaw"), field("Roll")
            if type(p) == "number" and type(yw) == "number" and type(r) == "number" then
                return string.format("(P=%s Y=%s R=%s)", num(p), num(yw), num(r))
            end
            local cr, cg, cb, ca = field("R"), field("G"), field("B"), field("A")
            if type(cr) == "number" and type(cg) == "number" and type(cb) == "number" and type(ca) == "number" then
                return string.format("(R=%s G=%s B=%s A=%s)", num(cr), num(cg), num(cb), num(ca))
            end
            if ut == "UScriptStruct" then return "struct" end
            local valid = try(function() return v:IsValid() end)
            if valid == false then return "(invalid " .. ut .. ")" end
            if valid == true then
                local name = try(function() return v:GetFullName() end)
                if name then return name end
            end
            local s = try(function() return v:ToString() end)
            if type(s) == "string" then return '"' .. s .. '"' end
            return ut
        end

        local function pclass(p) return try(function() return p:GetClass():GetFName():ToString() end) or "?" end

        -- A property's type as C++-ish text, and its property class.
        local ptype
        ptype = function(p)
            local k = pclass(p)
            if SHORT[k] then
                if k == "ByteProperty" then
                    local e = try(function() return fname(p:GetEnum()) end)
                    if e then return e, k end
                end
                return SHORT[k], k
            end
            if k == "StructProperty" then
                return (try(function() return fname(p:GetStruct()) end) or "struct"), k
            end
            if k == "EnumProperty" then
                return (try(function() return fname(p:GetEnum()) end) or "enum"), k
            end
            if k == "ArrayProperty" then
                local inner = try(function() return p:GetInner() end)
                return "TArray<" .. (inner and (ptype(inner)) or "?") .. ">", k
            end
            if OBJECT_PROPS[k] then
                local c = try(function() return fname(p:GetPropertyClass()) end) or "UObject"
                local wrap = ({ ClassProperty = "TSubclassOf", WeakObjectProperty = "TWeakObjectPtr",
                    SoftObjectProperty = "TSoftObjectPtr", SoftClassProperty = "TSoftClassPtr",
                    LazyObjectProperty = "TLazyObjectPtr", InterfaceProperty = "TScriptInterface" })[k]
                return wrap and (wrap .. "<" .. c .. ">") or (c .. "*"), k
            end
            return (k:gsub("Property$", "")), k
        end

        -- ------------------------------------------------------------------------------------------ references
        local function local_pc()
            local list = try(function() return FindAllOf("PlayerController") end) or {}
            local first
            for _, pc in ipairs(list) do
                if is_valid(pc) then
                    first = first or pc
                    if try(function() return pc:IsLocalController() end) == true then return pc end
                end
            end
            return first
        end

        local function valid_or_nil(o)
            if o ~= nil and is_valid(o) then return o end
        end

        local function resolve(ref)
            if type(ref) ~= "string" then
                if ref ~= nil and is_valid(ref) then return ref end
                return nil, "not an object reference: " .. tostring(ref)
            end
            if ref == "pc" then
                local pc = local_pc()
                if pc then return pc end
                return nil, "no player controller"
            end
            if ref == "pawn" then
                local pc = local_pc()
                local pawn = pc and valid_or_nil(try(function() return pc.Pawn end))
                if pawn then return pawn end
                return nil, "the player controller has no pawn"
            end
            if ref == "world" then
                local pc = local_pc()
                local w = pc and valid_or_nil(try(function() return pc:GetWorld() end))
                w = w or valid_or_nil(try(function() return FindFirstOf("World") end))
                if w then return w end
                return nil, "no world"
            end
            local kind, rest = ref:match("^(%a+):(.+)$")
            if kind == "first" then
                local o = valid_or_nil(try(function() return FindFirstOf(rest) end))
                if o then return o end
                return nil, "no live instance of " .. rest
            end
            if kind == "class" or kind == "cdo" then
                local cls = valid_or_nil(try(function() return StaticFindObject(rest) end))
                if not cls then return nil, "no class at " .. rest end
                if kind == "class" then return cls end
                local cdo = valid_or_nil(try(function() return cls:GetCDO() end))
                if not cdo then
                    local path = rest:gsub("%.([^.]+)$", ".Default__%1")
                    cdo = valid_or_nil(try(function() return StaticFindObject(path) end))
                end
                if cdo then return cdo end
                return nil, "no default object for " .. rest
            end
            if ref:sub(1, 1) == "/" then
                local o = valid_or_nil(try(function() return StaticFindObject(ref) end))
                if o then return o end
                return nil, "nothing at " .. ref
            end
            return nil, "unknown reference '" .. ref .. "' (use a /Path, first:Class, cdo:/Path, class:/Path, pc, pawn or world)"
        end
        I.resolve = resolve

        local function need(ref)
            local o, err = resolve(ref)
            if not o then error(err, 0) end
            return o
        end

        local function class_chain(obj)
            local cls = utype(obj) == "UClass" and obj or try(function() return obj:GetClass() end)
            local chain, depth = {}, 0
            while cls ~= nil and depth < 32 and is_valid(cls) do
                chain[#chain + 1] = cls
                cls = try(function() return cls:GetSuperStruct() end)
                depth = depth + 1
            end
            return chain
        end

        -- ------------------------------------------------------------------------------------------ find
        function I.find(spec, limit)
            limit = limit or 50
            local kind, rest = tostring(spec):match("^(%a+):(.+)$")
            local list
            if kind == "all" then
                list = try(function() return FindAllOf(rest) end) or {}
            else
                local o, err = resolve(spec)
                if not o then error(err, 0) end
                list = { o }
            end
            local lines = {}
            for i, o in ipairs(list) do
                if i > limit then break end
                lines[#lines + 1] = line("X", tostring(is_valid(o)), fullname(o))
            end
            if #list > limit then lines[#lines + 1] = line("N", #list, limit) end
            if #list == 0 then lines[#lines + 1] = line("W", "no live instance of " .. tostring(rest)) end
            return pack(lines)
        end

        -- ------------------------------------------------------------------------------------------ properties
        local function read(obj, name, k)
            if not READABLE[k] then return "(not read: " .. k .. ")" end
            local ok, v = pcall(function() return obj[name] end)
            if not ok then return "(error: " .. tostring(v) .. ")" end
            return show(v, k == "ArrayProperty" and 1 or 2)
        end
        I.read = read

        local function each_property(obj, super, f)
            for i, cls in ipairs(class_chain(obj)) do
                if i > 1 and not super then break end
                local owner = fname(cls) or "?"
                pcall(function() cls:ForEachProperty(function(p) return f(owner, p) end) end)
            end
        end

        function I.props(ref, pattern, super, limit)
            local obj = need(ref)
            local match, lines, total = matcher(pattern), { line("O", fullname(obj)) }, 0
            limit = limit or 400
            local is_class = utype(obj) == "UClass"
            local seen = {}
            each_property(obj, super ~= false, function(owner, p)
                local name = fname(p)
                if name and not seen[name] and match(name) then
                    seen[name] = true
                    total = total + 1
                    if total <= limit then
                        local t, k = ptype(p)
                        lines[#lines + 1] = line("P", owner, name, t, is_class and "" or read(obj, name, k))
                    end
                end
            end)
            if total > limit then lines[#lines + 1] = line("N", total, limit) end
            return pack(lines)
        end

        -- ------------------------------------------------------------------------------------------ signatures
        local function flags_text(f)
            local flags = try(function() return f:GetFunctionFlags() end)
            if type(flags) ~= "number" then return "" end
            local parts = {}
            for _, pair in ipairs(FUNC_FLAGS) do
                if flags & pair[1] ~= 0 then parts[#parts + 1] = pair[2] end
            end
            return table.concat(parts, ",")
        end

        -- The parameters of a UFunction, read by reflection. UE4SS's Lua has no property flags, so: a native return
        -- value is recognised by its name, Blueprint locals are cut at the first compiler-generated name, and an out
        -- parameter cannot be told from an in parameter (the reflection index can: `uvmod api`).
        local function params_of(f)
            local params, done = {}, false
            local ok, err = pcall(function()
                f:ForEachProperty(function(p)
                    if done then return end
                    local name = fname(p) or "?"
                    for _, pre in ipairs(LOCAL_PREFIXES) do
                        if name:sub(1, #pre) == pre then done = true return end
                    end
                    local t, k = ptype(p)
                    params[#params + 1] = { name = name, type = t, class = k, ret = name == "ReturnValue" }
                end)
            end)
            if not ok then return nil, tostring(err) end
            return params
        end
        I.params_of = params_of

        local function signature_text(params)
            local ins, ret = {}, nil
            for _, p in ipairs(params) do
                if p.ret then ret = p.type else ins[#ins + 1] = p.type .. " " .. p.name end
            end
            return "(" .. table.concat(ins, ", ") .. ")" .. (ret and (" -> " .. ret) or "")
        end

        local function find_function(obj, name)
            for _, cls in ipairs(class_chain(obj)) do
                local found
                pcall(function()
                    cls:ForEachFunction(function(f)
                        if not found and fname(f) == name then found = f end
                    end)
                end)
                if found then return found, fname(cls) end
            end
        end

        function I.functions(ref, pattern, super, limit)
            local obj = need(ref)
            local match, lines, total = matcher(pattern), { line("O", fullname(obj)) }, 0
            limit = limit or 300
            local seen = {}
            for i, cls in ipairs(class_chain(obj)) do
                if i > 1 and super == false then break end
                local owner = fname(cls) or "?"
                pcall(function()
                    cls:ForEachFunction(function(f)
                        local name = fname(f)
                        if name and not seen[name] and match(name) then
                            seen[name] = true
                            total = total + 1
                            if total <= limit then
                                local params = params_of(f)
                                lines[#lines + 1] = line("F", owner, name, flags_text(f),
                                    params and signature_text(params) or "(?)")
                            end
                        end
                    end)
                end)
            end
            if total > limit then lines[#lines + 1] = line("N", total, limit) end
            return pack(lines)
        end

        function I.signature(ref, name)
            local obj = need(ref)
            local f, owner = find_function(obj, name)
            if not f then error("no function " .. tostring(name) .. " on " .. fullname(obj), 0) end
            local params, err = params_of(f)
            if not params then error("cannot read the parameters of " .. name .. ": " .. err, 0) end
            local lines = { line("O", fullname(f)), line("F", owner, name, flags_text(f), signature_text(params)) }
            for i, p in ipairs(params) do
                lines[#lines + 1] = line("A", i, p.name, p.type, p.ret and "ret" or "in", p.class)
            end
            return pack(lines)
        end

        -- ------------------------------------------------------------------------------------------ guarded call
        -- Convert and check the arguments for a call; returns the converted list, or nil and the problems.
        -- Refused: nil, a missing value or anything but text for an FName (a native crash in UE4SS, not a Lua error),
        -- a non-table for a struct, an invalid object. Text becomes an FName / FText / object where the parameter
        -- needs one, so an agent can pass plain JSON.
        local function prepare(params, args)
            local inputs = {}
            for _, p in ipairs(params) do
                if not p.ret then inputs[#inputs + 1] = p end
            end
            local out, problems = {}, {}
            for i = 1, math.max(args.n, #inputs) do
                local p, v = inputs[i], args[i]
                if p == nil then
                    out[i] = v
                elseif i > args.n then
                    if p.class == "NameProperty" then
                        problems[#problems + 1] = string.format("argument %d (%s) is an FName and missing", i, p.name)
                    end
                elseif p.class == "NameProperty" then
                    if is_fname(v) then out[i] = v
                    elseif type(v) == "string" then out[i] = FName(v)
                    else problems[#problems + 1] = string.format("argument %d (%s) is an FName: pass text, not %s", i, p.name, type(v)) end
                elseif p.class == "TextProperty" and type(v) == "string" then
                    out[i] = try(function() return FText(v) end) or v
                elseif OBJECT_PROPS[p.class] and type(v) == "string" then
                    local o, err = resolve(v)
                    if o then out[i] = o else problems[#problems + 1] = string.format("argument %d (%s): %s", i, p.name, err) end
                elseif p.class == "StructProperty" and type(v) ~= "table" and type(v) ~= "userdata" then
                    problems[#problems + 1] = string.format("argument %d (%s) is a %s struct: pass a table, not %s", i, p.name, p.type, type(v))
                elseif type(utype(v)) == "string" and try(function() return v:IsValid() end) == false then
                    problems[#problems + 1] = string.format("argument %d (%s) is an invalid object", i, p.name)
                else
                    out[i] = v
                end
            end
            return out, problems
        end
        I.prepare = prepare

        -- args_src: a Lua expression list, evaluated here (so FName(...), {X = 1}, FindFirstOf(...) all work).
        function I.call(ref, name, args_src)
            local obj = need(ref)
            local f, owner = find_function(obj, name)
            if not f then error("no function " .. tostring(name) .. " on " .. fullname(obj), 0) end
            local params, err = params_of(f)
            if not params then error("cannot read the parameters of " .. name .. ": " .. err, 0) end
            local args = table.pack()
            if args_src and args_src ~= "" then
                local chunk, cerr = load("return " .. args_src, "=args", "t", ENV)
                if not chunk then error("the arguments do not compile: " .. tostring(cerr), 0) end
                args = table.pack(chunk())
            end
            local values, problems = prepare(params, args)
            if #problems > 0 then
                error("REFUSED " .. name .. signature_text(params) .. ": " .. table.concat(problems, "; "), 0)
            end
            local n = math.max(args.n, #values)
            local fn = obj[name]
            local results = table.pack(fn(obj, table.unpack(values, 1, n)))
            local lines = { line("O", fullname(obj)), line("F", owner, name, flags_text(f), signature_text(params)) }
            for i = 1, results.n do lines[#lines + 1] = line("V", "=> " .. show(results[i])) end
            for i = 1, n do
                if type(values[i]) == "table" and utype(values[i]) == nil then
                    lines[#lines + 1] = line("V", string.format("argument %d after the call: %s", i, show(values[i])))
                end
            end
            return pack(lines)
        end

        -- ------------------------------------------------------------------------------------------ snapshots
        local function walk(obj, pattern)
            local match, values, n = matcher(pattern), {}, 0
            each_property(obj, true, function(_, p)
                if n >= 2000 then return end
                local name = fname(p)
                if name and values[name] == nil and match(name) then
                    local _, k = ptype(p)
                    if READABLE[k] then
                        values[name] = read(obj, name, k)
                        n = n + 1
                    end
                end
            end)
            return values, n
        end

        function I.snapshot(label, ref, pattern)
            if type(label) ~= "string" or not label:match("^[%w_.-]+$") then error("a label is letters, digits, _ . -", 0) end
            local obj = need(ref)
            local values, n = walk(obj, pattern)
            I.snaps[label] = { ref = ref, pattern = pattern, obj = obj, name = fullname(obj), values = values, count = n,
                taken = os.time() }
            return pack({ line("O", fullname(obj)), line("S", label, os.time(), n, ref) })
        end

        function I.diff(label, ref, update)
            local s = I.snaps[label]
            if not s then error("no snapshot named " .. tostring(label), 0) end
            local obj
            if ref and ref ~= "" then obj = need(ref)
            elseif is_valid(s.obj) then obj = s.obj
            else obj = need(s.ref) end
            local now = walk(obj, s.pattern)
            local changed = {}
            for k, v in pairs(now) do
                local before = s.values[k]
                if before == nil then changed[#changed + 1] = { k, "(new)", v }
                elseif before ~= v then changed[#changed + 1] = { k, before, v } end
            end
            for k, v in pairs(s.values) do
                if now[k] == nil then changed[#changed + 1] = { k, v, "(gone)" } end
            end
            table.sort(changed, function(a, b) return a[1] < b[1] end)
            local lines = { line("O", fullname(obj)), line("N", #changed, #changed) }
            for _, c in ipairs(changed) do lines[#lines + 1] = line("D", c[1], c[2], c[3]) end
            if update then
                s.values, s.obj, s.name, s.taken = now, obj, fullname(obj), os.time()
            end
            return pack(lines)
        end

        function I.snapshots()
            local lines, labels = {}, {}
            for label in pairs(I.snaps) do labels[#labels + 1] = label end
            table.sort(labels)
            for _, label in ipairs(labels) do
                local s = I.snaps[label]
                lines[#lines + 1] = line("S", label, s.taken, s.count, s.ref)
            end
            return pack(lines)
        end

        function I.forget(label)
            if label == nil or label == "*" then I.snaps = {} else I.snaps[label] = nil end
            return I.snapshots()
        end

        -- ------------------------------------------------------------------------------------------ classes
        -- Loaded classes (kind "class"), structs ("struct") or enums ("enum") whose name contains `pattern`,
        -- optionally only subclasses of `base` (a ref).
        function I.classes(pattern, base, kind, limit)
            limit = limit or 200
            kind = kind or "class"
            local which = ({ class = "UClass", struct = "UScriptStruct", enum = "UEnum" })[kind]
            if not which then error("kind is class, struct or enum", 0) end
            local base_cls = nil
            if base and base ~= "" then
                base_cls = need(base:find(":", 1, true) and base or ("class:" .. base))
                if utype(base_cls) ~= "UClass" then base_cls = try(function() return base_cls:GetClass() end) end
            end
            local match = matcher(pattern)
            -- FindAllOf skips UClass objects; walking GUObjectArray is fast (64k objects in ~70 ms, UE 5.6).
            local found = {}
            ForEachUObject(function(o)
                if utype(o) == which then
                    local name = fname(o)
                    if name and match(name) then found[#found + 1] = o end
                end
            end)
            local rows = {}
            for _, c in ipairs(found) do
                if not base_cls or try(function() return c:IsChildOf(base_cls) end) == true then
                    local super = which ~= "UEnum" and try(function() return fname(c:GetSuperStruct()) end) or ""
                    local path = try(function() return c:GetFullName():match("^%S+%s+(.+)$") end) or fullname(c)
                    rows[#rows + 1] = { fname(c), super or "", path }
                end
            end
            table.sort(rows, function(a, b) return a[3] < b[3] end)
            local lines = {}
            for i, r in ipairs(rows) do
                if i > limit then break end
                lines[#lines + 1] = line("K", r[1], r[2], r[3])
            end
            if #rows > limit then lines[#lines + 1] = line("N", #rows, limit) end
            if #rows == 0 then lines[#lines + 1] = line("W", "no loaded " .. kind .. " matches") end
            return pack(lines)
        end

        -- ------------------------------------------------------------------------------------------ world, target
        function I.world()
            local lines = {}
            local pc = local_pc()
            local w = pc and valid_or_nil(try(function() return pc:GetWorld() end)) or valid_or_nil(try(function() return FindFirstOf("World") end))
            lines[#lines + 1] = line("V", "world: " .. (w and fullname(w) or "?"))
            lines[#lines + 1] = line("V", "player controller: " .. (pc and fullname(pc) or "none"))
            local pawn = pc and valid_or_nil(try(function() return pc.Pawn end))
            lines[#lines + 1] = line("V", "pawn: " .. (pawn and fullname(pawn) or "none"))
            if pawn then
                local loc = try(function() return pawn:K2_GetActorLocation() end)
                if loc then lines[#lines + 1] = line("V", "pawn location: " .. show(loc)) end
            end
            for _, cls in ipairs({ "GameModeBase", "GameStateBase", "GameInstance", "HUD" }) do
                local o = valid_or_nil(try(function() return FindFirstOf(cls) end))
                lines[#lines + 1] = line("V", cls .. ": " .. (o and fullname(o) or "none"))
            end
            return pack(lines)
        end

        local function lib(name)
            return valid_or_nil(try(function() return StaticFindObject("/Script/Engine.Default__" .. name) end))
        end

        -- What the camera looks at: a line trace from the camera (or from `ref`'s location along its forward
        -- vector) over `distance` cm on the Visibility channel.
        function I.target(distance, ref)
            distance = distance or 5000
            local ksl, kml = lib("KismetSystemLibrary"), lib("KismetMathLibrary")
            if not ksl or not kml then error("KismetSystemLibrary / KismetMathLibrary not found", 0) end
            local pc = local_pc()
            if not pc then error("no player controller", 0) end
            local start, rot, ignore
            if ref and ref ~= "" then
                local a = need(ref)
                start = a:K2_GetActorLocation()
                rot = a:K2_GetActorRotation()
                ignore = { a }
            else
                local cam = valid_or_nil(try(function() return pc.PlayerCameraManager end))
                if not cam then error("the player controller has no camera manager", 0) end
                start = cam:GetCameraLocation()
                rot = cam:GetCameraRotation()
                local pawn = valid_or_nil(try(function() return pc.Pawn end))
                ignore = pawn and { pawn } or {}
            end
            local fwd = kml:GetForwardVector(rot)
            local stop = { X = start.X + fwd.X * distance, Y = start.Y + fwd.Y * distance, Z = start.Z + fwd.Z * distance }
            local hit = {}
            local white = { R = 1, G = 1, B = 1, A = 1 }
            -- (WorldContext, Start, End, TraceChannel 0 = Visibility, bTraceComplex, ActorsToIgnore,
            --  DrawDebugType 0 = None, OutHit, bIgnoreSelf, TraceColor, TraceHitColor, DrawTime)
            local ok = ksl:LineTraceSingle(pc, { X = start.X, Y = start.Y, Z = start.Z }, stop, 0, false, ignore, 0, hit, true,
                white, white, 0)
            local lines = { line("V", "from: " .. show(start) .. " looking " .. show(rot)) }
            if not ok then
                lines[#lines + 1] = line("V", string.format("nothing hit within %g cm", distance))
                return pack(lines)
            end
            local function field(name)
                local v = rawget(hit, name)
                if v == nil then v = try(function() return hit[name] end) end
                return v
            end
            local comp = field("Component")
            if type(comp) == "userdata" and utype(comp) == "FWeakObjectPtr" then comp = try(function() return comp:Get() end) end
            local actor
            local handle = field("HitObjectHandle")
            if type(handle) == "table" then actor = rawget(handle, "Actor") end
            if type(actor) == "userdata" and utype(actor) == "FWeakObjectPtr" then actor = try(function() return actor:Get() end) end
            if actor == nil or not is_valid(actor) then actor = field("Actor") end
            if (actor == nil or not is_valid(actor)) and comp and is_valid(comp) then actor = try(function() return comp:GetOwner() end) end
            lines[#lines + 1] = line("V", "actor: " .. (actor and is_valid(actor) and fullname(actor) or "?"))
            lines[#lines + 1] = line("V", "component: " .. (comp and is_valid(comp) and fullname(comp) or "?"))
            for _, name in ipairs({ "Distance", "ImpactPoint", "ImpactNormal", "BoneName" }) do
                local v = field(name)
                if v ~= nil then lines[#lines + 1] = line("V", name .. ": " .. show(v)) end
            end
            return pack(lines)
        end

        __uvmod_inspect = I
    end
end
