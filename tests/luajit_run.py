"""Runs a Lua script under LuaJIT (CET's Lua) through lupa: python tests/luajit_run.py SCRIPT [ARG ...].
Needs `pip install lupa`; the tests use it when it is there (see test_cet_e2e.py)."""

import sys

from lupa import luajit21

lua = luajit21.LuaRuntime()
lua.execute("arg = {...}; arg[0] = table.remove(arg, 1); dofile(arg[0])", *sys.argv[1:])
