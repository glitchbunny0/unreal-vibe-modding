-- Tests for {{NAME}} without the game. Run from the project folder: lua tests/test_mod.lua (tests/run.sh runs all).
local mock = dofile("tests/ue4ss.lua")
local check = mock.check

-- The world the mod sees: a local player controller with a pawn.
local pawn = mock.object{
    path = "/Game/Maps/Test.Test:PersistentLevel.BP_Player_C_0", class = "BP_Player_C", supers = { "Pawn", "Actor" },
    methods = { K2_GetActorLocation = function() return { X = 1, Y = 2, Z = 3 } end },
}
mock.object{
    path = "/Game/Maps/Test.Test:PersistentLevel.PlayerController_0", class = "PlayerController",
    props = { Pawn = pawn }, methods = { IsLocalController = function() return true end },
}

mock.load_mod("{{NAME}}")
check("the mod loads", mock.find_log("loaded (hot-swap shell 1)"), mock.last_log())
check("F8 is bound", mock.keys.F8 ~= nil)

mock.press("F8")
check("F8 says where the player is", mock.last_log():find("press 1: the player is at 1 2 3", 1, true), mock.last_log())
mock.press("F8")
check("state survives between presses", mock.last_log():find("press 2:", 1, true), mock.last_log())

pawn.Destroy()
mock.press("F8")
check("a destroyed pawn is not touched", #mock.crashes == 0 and mock.last_log():find("no pawn yet", 1, true), mock.last_log())

mock.done()
