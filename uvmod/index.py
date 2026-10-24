"""The reflection index: the game's own reflection dump (UE4SS's DumpJMAP, the .jmap format by trumank) turned into
a SQLite database. It answers "which functions have Plank in their name", "which parameters of X are out
parameters", "what is Y's default" without touching the game, and it backs `uvmod check`. Standard library only."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE types(path TEXT PRIMARY KEY, kind TEXT, name TEXT, lname TEXT, super TEXT, flags TEXT, blueprint INTEGER);
CREATE TABLE functions(path TEXT PRIMARY KEY, owner TEXT, name TEXT, lname TEXT, flags TEXT, signature TEXT,
                       inputs INTEGER, script_bytes INTEGER);
CREATE TABLE members(owner TEXT, idx INTEGER, name TEXT, lname TEXT, type TEXT, kind TEXT, flags TEXT, role TEXT,
                     offset INTEGER, size INTEGER);
CREATE TABLE defaults(class TEXT, name TEXT, value TEXT);
CREATE TABLE enum_values(enum TEXT, idx INTEGER, name TEXT, value INTEGER);
CREATE INDEX types_lname ON types(lname);
CREATE INDEX types_super ON types(super);
CREATE INDEX functions_lname ON functions(lname);
CREATE INDEX functions_owner ON functions(owner);
CREATE INDEX members_owner ON members(owner);
CREATE INDEX members_lname ON members(lname);
CREATE INDEX defaults_class ON defaults(class);
"""

PRIMITIVES = {
    "BoolProperty": "bool", "ByteProperty": "uint8", "Int8Property": "int8", "Int16Property": "int16",
    "IntProperty": "int32", "Int64Property": "int64", "UInt16Property": "uint16", "UInt32Property": "uint32",
    "UInt64Property": "uint64", "FloatProperty": "float", "DoubleProperty": "double", "NameProperty": "FName",
    "StrProperty": "FString", "TextProperty": "FText", "FUtf8StrProperty": "FUtf8String", "FieldPathProperty": "FFieldPath",
}
WRAPPED = {"ClassProperty": ("TSubclassOf", "meta_class"), "SoftClassProperty": ("TSoftClassPtr", "meta_class"),
           "SoftObjectProperty": ("TSoftObjectPtr", "property_class"), "WeakObjectProperty": ("TWeakObjectPtr", "property_class"),
           "LazyObjectProperty": ("TLazyObjectPtr", "property_class"), "InterfaceProperty": ("TScriptInterface", "interface_class")}
FUNC_SHORT = [("FUNC_Native", "native"), ("FUNC_Static", "static"), ("FUNC_BlueprintCallable", "callable"),
              ("FUNC_BlueprintEvent", "event"), ("FUNC_BlueprintPure", "pure"), ("FUNC_Const", "const"),
              ("FUNC_Net", "net"), ("FUNC_NetServer", "server"), ("FUNC_NetClient", "client"),
              ("FUNC_NetMulticast", "multicast")]


def short(path: str | None) -> str:
    if not path:
        return "?"
    return re.split(r"[.:]", path)[-1]


def type_text(prop: dict[str, Any]) -> str:
    k = prop.get("type", "?")
    if k == "ByteProperty" and prop.get("enum"):
        return short(prop["enum"])
    if k in PRIMITIVES:
        return PRIMITIVES[k]
    if k == "StructProperty":
        return short(prop.get("struct"))
    if k == "ObjectProperty":
        return short(prop.get("property_class")) + "*"
    if k in WRAPPED:
        wrap, key = WRAPPED[k]
        return f"{wrap}<{short(prop.get(key))}>"
    if k == "EnumProperty":
        return short(prop.get("enum"))
    if k in ("ArrayProperty", "OptionalProperty"):
        return ("TArray" if k == "ArrayProperty" else "TOptional") + f"<{type_text(prop.get('inner') or {})}>"
    if k == "SetProperty":
        return f"TSet<{type_text(prop.get('key_prop') or {})}>"
    if k == "MapProperty":
        return f"TMap<{type_text(prop.get('key_prop') or {})}, {type_text(prop.get('value_prop') or {})}>"
    if "Delegate" in k:
        return f"{k.replace('Property', '')}<{short(prop.get('signature_function'))}>"
    return k.replace("Property", "")


def role_of(flags: str) -> str:
    """A function member's role: in, out (filled by the call), ref (in and out), ret, or local (not a parameter)."""
    f = set(x.strip() for x in flags.split("|"))
    if "CPF_Parm" not in f:
        return "local"
    if "CPF_ReturnParm" in f:
        return "ret"
    if "CPF_OutParm" in f:
        if "CPF_ConstParm" in f:
            return "in"  # a const reference is passed in
        return "ref" if "CPF_ReferenceParm" in f else "out"
    return "in"


def signature_text(params: list[tuple[str, str, str]]) -> str:
    """params: (name, type, role)."""
    ins = [(f"{role} " if role in ("out", "ref") else "") + f"{t} {n}" for n, t, role in params if role not in ("ret", "local")]
    ret = next((t for n, t, role in params if role == "ret"), None)
    return "(" + ", ".join(ins) + ")" + (f" -> {ret}" if ret else "")


def func_flags(flags: str) -> str:
    return ",".join(s for f, s in FUNC_SHORT if f in flags.split(" | "))


# --- building -------------------------------------------------------------------------------------------------

def build(jmap_path: str, db_path: str) -> dict[str, int]:
    """Build the index from a .jmap file. Returns counts."""
    with open(jmap_path, "rb") as f:
        data = json.load(f)
    objects: dict[str, dict[str, Any]] = data.get("objects", {})
    meta = data.get("metadata", {})
    tmp = db_path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    types, funcs, members, defaults, enums = [], [], [], [], []
    for path, o in objects.items():
        kind = o.get("type")
        if kind in ("Class", "ScriptStruct", "Enum"):
            name = short(path)
            flags = o.get("class_flags") or o.get("struct_flags") or o.get("enum_flags") or ""
            types.append((path, kind, name, name.lower(), o.get("super_struct"), flags, int(path.startswith("/Game/"))))
            if kind == "Enum":
                for i, pair in enumerate(o.get("names") or []):
                    enums.append((path, i, str(pair[0]), int(pair[1])))
            else:
                for i, p in enumerate(o.get("properties") or []):
                    members.append((path, i, p.get("name", "?"), p.get("name", "?").lower(), type_text(p), p.get("type", "?"),
                                    p.get("flags", ""), "prop", p.get("offset"), p.get("size")))
        elif kind == "Function":
            params = []
            for i, p in enumerate(o.get("properties") or []):
                role = role_of(p.get("flags", ""))
                t = type_text(p)
                params.append((p.get("name", "?"), t, role))
                members.append((path, i, p.get("name", "?"), p.get("name", "?").lower(), t, p.get("type", "?"),
                                p.get("flags", ""), role, p.get("offset"), p.get("size")))
            name = short(path)
            script = o.get("script") or ""
            funcs.append((path, o.get("outer"), name, name.lower(), func_flags(o.get("function_flags", "")),
                          signature_text(params), sum(1 for _, _, r in params if r not in ("ret", "local")),
                          len(script) * 3 // 4))
        elif kind == "Object" and "RF_ClassDefaultObject" in o.get("object_flags", ""):
            cls = o.get("class")
            for k, v in (o.get("property_values") or {}).items():
                defaults.append((cls, k, json.dumps(v, ensure_ascii=False)))
    db.executemany("INSERT OR REPLACE INTO types VALUES (?,?,?,?,?,?,?)", types)
    db.executemany("INSERT OR REPLACE INTO functions VALUES (?,?,?,?,?,?,?,?)", funcs)
    db.executemany("INSERT INTO members VALUES (?,?,?,?,?,?,?,?,?,?)", members)
    db.executemany("INSERT INTO defaults VALUES (?,?,?)", defaults)
    db.executemany("INSERT INTO enum_values VALUES (?,?,?,?)", enums)
    source = str(meta.get("source", ""))
    ev = meta.get("engine_version") or {}
    for k, v in {"source": source, "project": re.sub(r"-(Win64|WinGDK)-Shipping(\.exe)?$", "", source, flags=re.I),
                 "engine": f"{ev.get('major', '?')}.{ev.get('minor', '?')}", "changelist": str(meta.get("build_change_list") or ""),
                 "dumped": str(meta.get("timestamp", "")), "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "jmap": os.path.basename(jmap_path)}.items():
        db.execute("INSERT INTO meta VALUES (?,?)", (k, v))
    db.commit()
    db.close()
    os.replace(tmp, db_path)
    return {"classes": sum(1 for t in types if t[1] == "Class"), "structs": sum(1 for t in types if t[1] == "ScriptStruct"),
            "enums": sum(1 for t in types if t[1] == "Enum"), "functions": len(funcs), "defaults": len(defaults)}


# --- finding an index -----------------------------------------------------------------------------------------

def folder() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "uvmod" / "index"


def project_of_ue4ss(ue4ss: str) -> str:
    """The project name in a ue4ss folder path: <...>/<Project>/Binaries/Win64/ue4ss."""
    parts = re.split(r"[\\/]+", ue4ss.rstrip("\\/"))
    for i, part in enumerate(parts):
        if part.lower() == "binaries" and i > 0:
            return parts[i - 1]
    return ""


def find(pinned: str = "", ue4ss: str = "") -> Path | None:
    """UVMOD_INDEX if set; else the newest index of the configured game's project; else the newest index."""
    if pinned:
        p = Path(pinned).expanduser()
        return p if p.exists() else None
    candidates = sorted(folder().glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    project = project_of_ue4ss(ue4ss).lower()
    if project:
        for c in candidates:
            try:
                with closing(sqlite3.connect(f"file:{c}?mode=ro", uri=True)) as db:
                    row = db.execute("SELECT value FROM meta WHERE key = 'project'").fetchone()
            except sqlite3.Error:
                continue
            if row and row[0].lower() == project:
                return c
    return candidates[0] if candidates else None


def index_name(jmap_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(jmap_name).stem) + ".sqlite"


# --- asking -----------------------------------------------------------------------------------------------------

@dataclass
class Param:
    name: str
    type: str
    kind: str
    role: str


class Index:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    def close(self) -> None:
        self.db.close()

    def meta(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key, value FROM meta"))

    def header(self) -> str:
        m = self.meta()
        return f"(index: {m.get('project', '?')}, UE {m.get('engine', '?')} CL {m.get('changelist', '?')}, dumped {m.get('dumped', '?')})"

    # -- lookups
    def resolve_type(self, spec: str, kinds: Iterable[str] = ("Class", "ScriptStruct", "Enum")) -> list[tuple[str, str]]:
        kinds = tuple(kinds)
        q = ",".join("?" * len(kinds))
        if spec.startswith("/"):
            return list(self.db.execute(f"SELECT path, kind FROM types WHERE path = ? AND kind IN ({q})", (spec, *kinds)))
        name = spec.lower()
        rows = list(self.db.execute(f"SELECT path, kind FROM types WHERE lname = ? AND kind IN ({q}) ORDER BY blueprint, path",
                                    (name, *kinds)))
        if not rows and not name.endswith("_c"):  # a Blueprint class by its asset name: BP_Door -> BP_Door_C
            rows = list(self.db.execute(f"SELECT path, kind FROM types WHERE lname = ? AND kind IN ({q})", (name + "_c", *kinds)))
        if not rows and name[:1] in ("a", "u", "f", "e"):  # C++ prefix: AActor -> Actor
            rows = list(self.db.execute(f"SELECT path, kind FROM types WHERE lname = ? AND kind IN ({q})", (name[1:], *kinds)))
        return rows

    def supers(self, path: str) -> list[str]:
        chain, seen = [], set()
        cur = path
        while cur and cur not in seen:
            seen.add(cur)
            row = self.db.execute("SELECT super FROM types WHERE path = ?", (cur,)).fetchone()
            cur = row[0] if row else None
            if cur:
                chain.append(cur)
        return chain

    def params(self, function_path: str) -> list[Param]:
        return [Param(*r) for r in self.db.execute(
            "SELECT name, type, kind, role FROM members WHERE owner = ? AND role != 'local' ORDER BY idx", (function_path,))]

    def functions_named(self, name: str) -> list[tuple[str, str]]:
        return list(self.db.execute("SELECT path, signature FROM functions WHERE lname = ? ORDER BY path", (name.lower(),)))

    # -- answers (text)
    def search(self, text: str, kind: str = "", limit: int = 40) -> list[str]:
        needle = text.lower()
        like = f"%{needle}%"
        rows: list[tuple[int, str]] = []

        def rank(name: str) -> int:
            n = name.lower()
            return 0 if n == needle else 1 if n.startswith(needle) else 2

        if kind in ("", "class", "struct", "enum", "type"):
            want = {"class": ("Class",), "struct": ("ScriptStruct",), "enum": ("Enum",)}.get(kind, ("Class", "ScriptStruct", "Enum"))
            q = ",".join("?" * len(want))
            for path, k, name in self.db.execute(f"SELECT path, kind, name FROM types WHERE lname LIKE ? AND kind IN ({q})",
                                                 (like, *want)):
                rows.append((rank(name), f"{k.lower().replace('scriptstruct', 'struct')} {path}"))
        if kind in ("", "function"):
            for path, name, sig, flags in self.db.execute(
                    "SELECT path, name, signature, flags FROM functions WHERE lname LIKE ?", (like,)):
                rows.append((rank(name), f"function {path}{sig}" + (f"  [{flags}]" if flags else "")))
        if kind in ("", "property"):
            for owner, name, t in self.db.execute(
                    "SELECT m.owner, m.name, m.type FROM members m JOIN types t ON t.path = m.owner "
                    "WHERE m.lname LIKE ? AND m.role = 'prop'", (like,)):
                rows.append((rank(name), f"property {owner}.{name}: {t}"))
        rows.sort(key=lambda r: (r[0], len(r[1]), r[1]))
        out = [r[1] for r in rows[:limit]]
        if len(rows) > limit:
            out.append(f"({len(rows)} matches, {limit} shown: narrow it, or --kind class|struct|enum|function|property)")
        return out or [f"nothing named like '{text}' in the index"]

    def describe(self, spec: str, inherited: bool = False, limit: int = 300) -> list[str]:
        found = self.resolve_type(spec)
        if not found:
            return [f"no class, struct or enum '{spec}' in the index (try: uvmod api search {spec})"]
        if len(found) > 1:
            return [f"'{spec}' names {len(found)} types; use a path:"] + [f"  {k.lower()} {p}" for p, k in found]
        path, kind = found[0]
        if kind == "Enum":
            return [f"enum {path}"] + [f"  {name} = {value}" for name, value in
                                       self.db.execute("SELECT name, value FROM enum_values WHERE enum = ? ORDER BY idx", (path,))]
        chain = self.supers(path)
        out = [f"{kind.lower().replace('scriptstruct', 'struct')} {path}" + (f" : {' : '.join(short(s) for s in chain)}" if chain else "")]
        owners = [path] + (chain if inherited else [])
        defaults = dict(self.db.execute("SELECT name, value FROM defaults WHERE class = ?", (path,)))
        shown = 0
        for owner in owners:
            props = list(self.db.execute("SELECT name, type FROM members WHERE owner = ? AND role = 'prop' ORDER BY idx", (owner,)))
            funcs = list(self.db.execute("SELECT name, signature, flags FROM functions WHERE owner = ? ORDER BY name", (owner,)))
            if not props and not funcs:
                continue
            out.append(f"{short(owner)}:")
            for name, t in props:
                shown += 1
                if shown > limit:
                    break
                d = defaults.get(name)
                out.append(f"  {name}: {t}" + (f" = {d}" if d not in (None, "null", "{}", "[]") else ""))
            for name, sig, flags in funcs:
                shown += 1
                if shown > limit:
                    break
                out.append(f"  {name}{sig}" + (f"  [{flags}]" if flags else ""))
        if shown > limit:
            out.append(f"(cut at {limit} lines)")
        if not inherited and chain:
            out.append("(inherited members: --inherited)")
        return out

    def signature(self, spec: str) -> list[str]:
        if ":" in spec:
            owner, _, name = spec.rpartition(":")
            owners = [p for p, _ in self.resolve_type(owner, ("Class", "ScriptStruct"))] if not owner.startswith("/") else [owner]
            rows = []
            for o in owners:
                for cls in [o] + self.supers(o):
                    rows += list(self.db.execute("SELECT path, flags FROM functions WHERE owner = ? AND lname = ?", (cls, name.lower())))
                    if rows:
                        break
        else:
            rows = list(self.db.execute("SELECT path, flags FROM functions WHERE lname = ? ORDER BY path", (spec.lower(),)))
        if not rows:
            return [f"no function '{spec}' in the index"]
        if len(rows) > 1:
            return [f"'{spec}' names {len(rows)} functions; use Class:Function:"] + [f"  {p}" for p, _ in rows[:30]]
        path, flags = rows[0]
        params = self.params(path)
        out = [f"{path}{signature_text([(p.name, p.type, p.role) for p in params])}" + (f"  [{flags}]" if flags else "")]
        n = 0
        for p in params:
            if p.role == "ret":
                out.append(f"  returns {p.type} ({p.kind})")
                continue
            n += 1
            note = {"out": "out: pass {} and read it after the call", "ref": "in and out: pass a value, read it after",
                    "in": "in"}[p.role]
            out.append(f"  {n}. {p.name}: {p.type} ({note}; {p.kind})")
        names = [p for p in params if p.kind == "NameProperty" and p.role != "ret"]
        if names:
            out.append("  FName parameters need FName(\"...\") in raw Lua (a string or nil there is a native crash); `call` converts text")
        return out

    def subclasses(self, spec: str, limit: int = 200) -> list[str]:
        found = self.resolve_type(spec, ("Class", "ScriptStruct"))
        if len(found) != 1:
            return self.describe(spec)[:20]
        root = found[0][0]
        out, frontier, seen = [], [root], {root}
        while frontier:
            nxt = []
            for p in frontier:
                for (child,) in self.db.execute("SELECT path FROM types WHERE super = ? ORDER BY path", (p,)):
                    if child not in seen:
                        seen.add(child)
                        out.append(child)
                        nxt.append(child)
            frontier = nxt
        out.sort()
        lines = [f"{len(out)} subclasses of {root}"] + [f"  {p}" for p in out[:limit]]
        if len(out) > limit:
            lines.append(f"  ({len(out) - limit} more)")
        return lines
