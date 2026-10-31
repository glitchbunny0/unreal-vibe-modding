## What this adds or fixes

<!-- One or two sentences. -->

## Evidence

<!-- Where you ran it: the game, its UE version and UE4SS build, or for Cyberpunk 2077 the patch and CET version.
     What it returned. For a skill rule: what you measured. -->

## Checklist

- [ ] `python -m unittest discover -s tests -v` passes (with Lua 5.4, and lupa installed for the CET bridge)
- [ ] A new CET handler is in `cetbridge_fixes.lua` and in `uvmod/cet.py` (`READS` or `CHANGES`), with a test
- [ ] No game files, extracted assets, machine names, user names or local paths
