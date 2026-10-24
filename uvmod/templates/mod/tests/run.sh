#!/usr/bin/env bash
# Everything that can be checked without the game: syntax, `uvmod check` (FName mistakes that crash UE4SS, when a
# reflection index exists for the game), and the tests against the strict UE4SS stand-in.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
LUA="${LUA:-$(command -v lua5.4 || command -v lua || true)}"
LUAC="${LUAC:-$(command -v luac5.4 || command -v luac || true)}"
[[ -n "$LUA" ]] || { echo "needs Lua 5.4 (set LUA=/path/to/lua)" >&2; exit 2; }
for f in {{NAME}}/Scripts/*.lua tests/*.lua; do
    if [[ -n "$LUAC" ]]; then "$LUAC" -p "$f"; else "$LUA" -e "assert(loadfile('$f'))"; fi
done
if command -v uvmod >/dev/null && uvmod api info >/dev/null 2>&1; then
    uvmod check {{NAME}}
else
    echo "(skipping uvmod check: no reflection index; run \`uvmod index\` with the game running)"
fi
for t in tests/test_*.lua; do "$LUA" "$t"; done
