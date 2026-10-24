--[[ A small fake of UE4SS's reflection API for the inspect tests, loaded by tests/fake_ue4ss.lua: classes with
     properties and functions, objects, FName/FText, FindFirstOf/FindAllOf/StaticFindObject, a player controller with
     a camera, and the Kismet libraries LineTraceSingle and GetForwardVector use.

     Calls follow UE4SS's rules: the arity must match (a Lua error otherwise), and anything but an FName for an FName
     parameter is a NATIVE CRASH: the fake game writes it to the log and exits, as the real game would die. ]]

local registry = {} -- path -> class / function / object
local instances = {} -- every live object, in creation order

local function name_of(s)
    return setmetatable({}, { __name = "FName", __index = {
        ToString = function() return s end,
        type = function() return "FName" end,
    }, __eq = function(a, b) return a:ToString() == b:ToString() end })
end

function FName(s)
    if type(s) ~= "string" then error("FName expects a string", 2) end
    return name_of(s)
end

function FText(s)
    return setmetatable({}, { __name = "FText", __index = { ToString = function() return s end, type = function() return "FText" end } })
end

local function field_class(k)
    return { GetFName = function() return name_of(k) end }
end

-- A property: Prop(name, class, {struct = cls, class = cls, inner = prop, enum = e})
function FakeProp(name, k, extra)
    extra = extra or {}
    return setmetatable({}, { __name = "Property", __index = {
        GetFName = function() return name_of(name) end,
        GetClass = function() return field_class(k) end,
        GetStruct = function() return extra.struct end,
        GetPropertyClass = function() return extra.class end,
        GetInner = function() return extra.inner end,
        GetEnum = function() return extra.enum end,
        type = function() return "Property" end,
    } })
end

local function crash(why)
    print("NATIVE CRASH: " .. why .. "\n")
    os.exit(3)
end

-- A UFunction: params is a list of FakeProp (a native return value is called ReturnValue); impl(self, ...) runs it.
function FakeFunction(owner_path, name, params, flags, impl)
    local f = { name = name, params = params, flags = flags or 0x04000400, impl = impl }
    local path = owner_path .. ":" .. name
    local mt = { __name = "UFunction" }
    mt.__index = {
        GetFName = function() return name_of(name) end,
        GetFullName = function() return "Function " .. path end,
        IsValid = function() return true end,
        GetFunctionFlags = function() return f.flags end,
        ForEachProperty = function(_, cb) for _, p in ipairs(params) do cb(p) end end,
        type = function() return "UFunction" end,
    }
    -- Called as obj[name](obj, ...): like UE4SS, a first argument that is the bound object is dropped.
    mt.__call = function(self, ...)
        local args = table.pack(...)
        local first = 1
        if self.bound ~= nil and args[1] == self.bound then first = 2 end
        local inputs = {}
        for _, p in ipairs(params) do
            local pname = p:GetFName():ToString()
            -- Blueprint locals live in the same property list but are not parameters (UE4SS reads CPF_Parm).
            if pname ~= "ReturnValue" and not pname:match("^CallFunc_") and not pname:match("^K2Node_") then
                inputs[#inputs + 1] = p
            end
        end
        local given = args.n - first + 1
        if given ~= #inputs then
            error(string.format("[UFunction::setup_metamethods -> __call] UFunction expected %d parameters, received %d", #inputs, given), 2)
        end
        local values = {}
        for i, p in ipairs(inputs) do
            local v = args[first + i - 1]
            local k = p:GetClass():GetFName():ToString()
            if k == "NameProperty" and not (type(v) == "table" and getmetatable(v) and getmetatable(v).__name == "FName") then
                crash(name .. " got " .. type(v) .. " for FName parameter " .. p:GetFName():ToString())
            end
            values[i] = v
        end
        return f.impl(self.bound, table.unpack(values, 1, #inputs))
    end
    local fn = setmetatable({}, mt)
    registry[path] = fn
    f.object = fn
    return fn
end

local function bind(fn, obj)
    return setmetatable({ bound = obj }, getmetatable(fn))
end

-- A class: FakeClass(path, super, props, functions)
function FakeClass(path, super, props, functions)
    local name = path:match("[.:]([^.:]+)$")
    local cls = { path = path, name = name, super = super, props = props or {}, functions = functions or {} }
    local mt = { __name = "UClass" }
    mt.__index = {
        GetFName = function() return name_of(name) end,
        GetFullName = function() return "Class " .. path end,
        IsValid = function() return true end,
        GetSuperStruct = function() return super end,
        ForEachProperty = function(_, cb) for _, p in ipairs(cls.props) do cb(p) end end,
        ForEachFunction = function(_, cb) for _, f in ipairs(cls.functions) do cb(f) end end,
        IsChildOf = function(self, other)
            local c = self
            while c do
                if c == other then return true end
                c = c:GetSuperStruct()
            end
            return false
        end,
        GetCDO = function() return cls.cdo end,
        GetClass = function() return registry["/Script/CoreUObject.Class"] end,
        type = function() return "UClass" end,
        __data = cls,
    }
    local c = setmetatable({}, mt)
    registry[path] = c
    return c
end

local function class_data(c) return getmetatable(c).__index.__data end

local function find_function(cls, name)
    local c = cls
    while c do
        for _, f in ipairs(class_data(c).functions) do
            if f:GetFName():ToString() == name then return f end
        end
        c = c:GetSuperStruct()
    end
end

local function is_a(cls, short)
    local c = cls
    while c do
        if class_data(c).name == short then return true end
        c = c:GetSuperStruct()
    end
    return false
end

-- An object: FakeInstance(cls, path, values); values hold property values, `valid = false` makes it dead.
function FakeInstance(cls, path, values, opts)
    opts = opts or {}
    local obj
    local state = { valid = true, values = values or {} }
    local methods = {
        GetFName = function() return name_of(path:match("[.:]([^.:]+)$")) end,
        GetFullName = function() return class_data(cls).name .. " " .. path end,
        IsValid = function() return state.valid end,
        GetClass = function() return cls end,
        type = function() return "UObject" end,
        FakeKill = function() state.valid = false end,
    }
    for k, v in pairs(opts.methods or {}) do methods[k] = v end
    obj = setmetatable({}, {
        __name = "UObject",
        __index = function(_, k)
            if methods[k] then return methods[k] end
            if not state.valid then error("NATIVE CRASH would follow: read " .. k .. " on a destroyed object", 2) end
            if state.values[k] ~= nil then return state.values[k] end
            local f = find_function(cls, k)
            if f then return bind(f, obj) end
            return nil
        end,
        __newindex = function(_, k, v) state.values[k] = v end,
    })
    registry[path] = obj
    if not opts.cdo then instances[#instances + 1] = { obj = obj, cls = cls } end
    return obj
end

local INVALID = setmetatable({}, { __name = "UObject", __index = {
    IsValid = function() return false end,
    GetFullName = function() return "" end,
    type = function() return "UObject" end,
} })

function FindFirstOf(short)
    for _, i in ipairs(instances) do
        if is_a(i.cls, short) and i.obj:IsValid() then return i.obj end
    end
    if short == "Class" then
        for _, v in pairs(registry) do if getmetatable(v).__name == "UClass" then return v end end
    end
    return INVALID
end

function FindAllOf(short)
    local out = {}
    if short == "Class" then
        for _, v in pairs(registry) do if getmetatable(v).__name == "UClass" then out[#out + 1] = v end end
    else
        for _, i in ipairs(instances) do
            if is_a(i.cls, short) then out[#out + 1] = i.obj end
        end
    end
    if #out == 0 then return nil end
    return out
end

function ForEachUObject(cb)
    local paths = {}
    for path in pairs(registry) do paths[#paths + 1] = path end
    table.sort(paths)
    for i, path in ipairs(paths) do cb(registry[path], 0, i) end
end

function StaticFindObject(path)
    return registry[path] or INVALID
end

-- --------------------------------------------------------------------------------------------- the fake game
local P = FakeProp
local Object = FakeClass("/Script/CoreUObject.Object", nil, {}, {})
FakeClass("/Script/CoreUObject.Class", Object, {}, {})
local Vector = FakeClass("/Script/CoreUObject.Vector", nil, { P("X", "DoubleProperty"), P("Y", "DoubleProperty"), P("Z", "DoubleProperty") })
local Rotator = FakeClass("/Script/CoreUObject.Rotator", nil, {})
local EMode = { GetFName = function() return name_of("EMovementMode") end }

local Actor = FakeClass("/Script/Engine.Actor", Object, {
    P("bHidden", "BoolProperty"),
    P("Tags", "ArrayProperty", { inner = P("Tags", "NameProperty") }),
}, {})
table.insert(class_data(Actor).functions, FakeFunction("/Script/Engine.Actor", "K2_GetActorLocation",
    { P("ReturnValue", "StructProperty", { struct = Vector }) }, 0x54020400, function(self) return self.Location or { X = 0, Y = 0, Z = 0 } end))
table.insert(class_data(Actor).functions, FakeFunction("/Script/Engine.Actor", "K2_GetActorRotation",
    { P("ReturnValue", "StructProperty", { struct = Rotator }) }, 0x54020400, function() return { Pitch = 0, Yaw = 0, Roll = 0 } end))
table.insert(class_data(Actor).functions, FakeFunction("/Script/Engine.Actor", "SetActorHiddenInGame",
    { P("bNewHidden", "BoolProperty") }, 0x04020400, function(self, hidden) self.bHidden = hidden end))
table.insert(class_data(Actor).functions, FakeFunction("/Script/Engine.Actor", "ActorHasTag",
    { P("Tag", "NameProperty"), P("ReturnValue", "BoolProperty") }, 0x54020400, function(self, tag)
        return tag:ToString() == "Plank"
    end))

local Pawn = FakeClass("/Script/Engine.Pawn", Actor, { P("Health", "FloatProperty"), P("MoveMode", "EnumProperty", { enum = EMode }) }, {})
local Plank = FakeClass("/Game/Fake/BP_Plank.BP_Plank_C", Actor, {
    P("HeldBy", "ObjectProperty", { class = Pawn }),
    P("Offset", "StructProperty", { struct = Vector }),
    P("Label", "StrProperty"),
    P("Pickup", "SoftObjectProperty", { class = Object }),
}, {})
table.insert(class_data(Plank).functions, FakeFunction("/Game/Fake/BP_Plank.BP_Plank_C", "Grab",
    { P("Who", "ObjectProperty", { class = Pawn }), P("Socket", "NameProperty"), P("CallFunc_Temp", "IntProperty") }, 0x04020400,
    function(self, who, socket) self.HeldBy = who self.Label = "held at " .. socket:ToString() end))
table.insert(class_data(Plank).functions, FakeFunction("/Game/Fake/BP_Plank.BP_Plank_C", "Nudge",
    { P("By", "StructProperty", { struct = Vector }) }, 0x04020400,
    function(self, by) local o = self.Offset self.Offset = { X = o.X + by.X, Y = o.Y + by.Y, Z = o.Z + by.Z } end))

local Camera = FakeClass("/Script/Engine.PlayerCameraManager", Actor, {}, {})
table.insert(class_data(Camera).functions, FakeFunction("/Script/Engine.PlayerCameraManager", "GetCameraLocation",
    { P("ReturnValue", "StructProperty", { struct = Vector }) }, 0x54020400, function() return { X = 0, Y = 0, Z = 100 } end))
table.insert(class_data(Camera).functions, FakeFunction("/Script/Engine.PlayerCameraManager", "GetCameraRotation",
    { P("ReturnValue", "StructProperty", { struct = Rotator }) }, 0x54020400, function() return { Pitch = 0, Yaw = 0, Roll = 0 } end))
local Controller = FakeClass("/Script/Engine.PlayerController", Actor, {
    P("Pawn", "ObjectProperty", { class = Pawn }),
    P("PlayerCameraManager", "ObjectProperty", { class = Camera }),
}, {})

local KSL = FakeClass("/Script/Engine.KismetSystemLibrary", Object, {}, {})
local KML = FakeClass("/Script/Engine.KismetMathLibrary", Object, {}, {})
table.insert(class_data(KML).functions, FakeFunction("/Script/Engine.KismetMathLibrary", "GetForwardVector",
    { P("InRot", "StructProperty", { struct = Rotator }), P("ReturnValue", "StructProperty", { struct = Vector }) }, 0x54022401,
    function() return { X = 1, Y = 0, Z = 0 } end))

local hero = FakeInstance(Pawn, "/Game/Maps/Test.Test:PersistentLevel.BP_Hero_C_0", { Health = 75.5, MoveMode = 1, bHidden = false,
    Location = { X = 10, Y = 20, Z = 30 } })
local plank = FakeInstance(Plank, "/Game/Maps/Test.Test:PersistentLevel.BP_Plank_C_3", { bHidden = false,
    Offset = { X = 1, Y = 2, Z = 3 }, Label = "a plank" })
local camera = FakeInstance(Camera, "/Game/Maps/Test.Test:PersistentLevel.PlayerCameraManager_0", {})
local World = FakeClass("/Script/Engine.World", Object, {}, {})
local world = FakeInstance(World, "/Game/Maps/Test.Test", {})
FakeInstance(Controller, "/Game/Maps/Test.Test:PersistentLevel.PlayerController_0", { Pawn = hero, PlayerCameraManager = camera },
    { methods = { IsLocalController = function() return true end, GetWorld = function() return world end } })
local ksl = FakeInstance(KSL, "/Script/Engine.Default__KismetSystemLibrary", {}, { cdo = true })
FakeInstance(KML, "/Script/Engine.Default__KismetMathLibrary", {}, { cdo = true })
class_data(KSL).cdo = ksl

local Hit = FakeClass("/Script/Engine.HitResult", nil, {})
table.insert(class_data(KSL).functions, FakeFunction("/Script/Engine.KismetSystemLibrary", "LineTraceSingle", {
    P("WorldContextObject", "ObjectProperty", { class = Object }), P("Start", "StructProperty", { struct = Vector }),
    P("End", "StructProperty", { struct = Vector }), P("TraceChannel", "ByteProperty"), P("bTraceComplex", "BoolProperty"),
    P("ActorsToIgnore", "ArrayProperty", { inner = P("ActorsToIgnore", "ObjectProperty", { class = Actor }) }),
    P("DrawDebugType", "ByteProperty"), P("OutHit", "StructProperty", { struct = Hit }), P("bIgnoreSelf", "BoolProperty"),
    P("TraceColor", "StructProperty"), P("TraceHitColor", "StructProperty"), P("DrawTime", "FloatProperty"),
    P("ReturnValue", "BoolProperty"),
}, 0x14422401, function(_, _, _, stop, _, _, _, _, hit)
    if stop.X < 1000 then return false end
    hit.HitObjectHandle = { Actor = plank }
    hit.Distance = 250.0
    hit.ImpactPoint = { X = 250, Y = 0, Z = 100 }
    return true
end))

function FakePlank() return plank end

-- DumpJMAP writes a small .jmap into the ue4ss folder, as UE4SS does (the format by trumank, a few objects only).
function DumpJMAP()
    local f = assert(io.open(FAKE_UE4SS_DIR .. "/FakeGame-5.6.0-1-fake.jmap", "w"))
    f:write([[{"metadata": {"source": "FakeGame-Win64-Shipping.exe", "engine_version": {"major": 5, "minor": 6},
 "build_change_list": "1", "timestamp": "2026-10-24T00:00:00Z"},
 "objects": {
  "/Script/Engine.Actor": {"type": "Class", "super_struct": "/Script/CoreUObject.Object", "properties": []},
  "/Script/Engine.Actor:ActorHasTag": {"type": "Function", "outer": "/Script/Engine.Actor", "function_flags": "FUNC_Native",
    "properties": [{"name": "Tag", "type": "NameProperty", "flags": "CPF_Parm"},
                   {"name": "ReturnValue", "type": "BoolProperty", "flags": "CPF_Parm | CPF_OutParm | CPF_ReturnParm"}]}
 }}]])
    f:close()
    print("[JMapGenerator] Dump complete: 2 objects\n")
end
