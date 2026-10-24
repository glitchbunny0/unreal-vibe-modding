--[[ {{NAME}}: the mod's logic. Edit this file and run `uvmod swap {{NAME}}` to load it into the running game.

    Rules (see main.lua for the contract): register nothing here (main.lua registers what KEYS and HOOKS declare);
    keep every bit of state in S, which survives swaps; call UFunctions the way `uvmod api sig` shows them, with
    FName("...") for FName parameters (a Lua string there is a native crash; `uvmod check` finds them).
--]]

local M = { SHELL = 1, VERSION = "0.1.0" }

M.KEYS = {
    { key = "F8", label = "say where the player is" },
}

-- Hooks the game calls into. Blueprint functions: a pre-hook only (UE4SS runs no post callback for them).
M.HOOKS = {
    -- { path = "/Game/Path/BP_Thing.BP_Thing_C:DoThing", handler = "on_do_thing" },
}

local S

function M.init(state)
    S = state
    S.presses = S.presses or 0
end

local function local_pawn()
    for _, pc in ipairs(FindAllOf("PlayerController") or {}) do
        if pc:IsValid() and pc:IsLocalController() then
            local pawn = pc.Pawn
            if pawn and pawn:IsValid() then return pawn end
        end
    end
end

function M.on_key(key)
    if key == "F8" then
        S.presses = S.presses + 1
        local pawn = local_pawn()
        if not pawn then
            print("[{{NAME}}] no pawn yet (main menu?)\n")
            return
        end
        local loc = pawn:K2_GetActorLocation()
        print(string.format("[{{NAME}}] press %d: the player is at %.0f %.0f %.0f\n", S.presses, loc.X, loc.Y, loc.Z))
    end
end

function M.tick() end

return M
