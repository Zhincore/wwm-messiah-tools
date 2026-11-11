#!/usr/bin/env python3
"""Build lua/luac 5.4 with the Messiah engine's custom opcode numbering.

Fetches stock Lua, renumbers the opcode table to match the game's, and builds.
Inserted opcodes are aliased to their stock base opcode, so the resulting
binaries load and run game bytecode directly, and emit it with game numbering.

Stdlib only. Needs `make` and a C compiler.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

LUA_VERSION = "5.4.7"
LUA_URL = f"https://www.lua.org/ftp/lua-{LUA_VERSION}.tar.gz"
LUA_SHA256 = "9fbf5e28ef86c69858f6d3d34eccc32e911c1a28b4120ff3e84aaa70cfbf1e30"

# anchor stock opcode -> [(inserted opcode, stock opcode whose semantics it shares)]
# Inserted opcodes follow their anchor, in this order.
INSERTIONS: dict[str, list[tuple[str, str]]] = {
    "GETTABUP": [("GETTABUPDOT", "GETTABUP"), ("GETTABUPFAST", "GETTABUP")],
    "GETFIELD": [("GETFIELDDOT", "GETFIELD"), ("GETFIELDFAST", "GETFIELD")],
    "SETTABUP": [("SETTABUPDOT", "SETTABUP")],
    "SETFIELD": [
        ("SETFIELDDOT", "SETFIELD"),
        ("SETDICTPROXYUP", "SETTABUP"),
        ("SETDICTPROXY", "SETTABLE"),
        ("SETDICTPROXYI", "SETI"),
        ("SETDICTPROXYFIELD", "SETFIELD"),
    ],
    "NEWTABLE": [
        ("NEWLIST", "NEWTABLE"),
        ("NEWDICT", "NEWTABLE"),
        ("NEWLISTPROXY", "NEWTABLE"),
        ("NEWDICTPROXY", "NEWTABLE"),
    ],
    "SETLIST": [("LISTSETLIST", "SETLIST")],
}


def fetch(dest: Path) -> Path:
    """Download the stock tarball unless already present, and verify it."""
    if not dest.exists():
        print(f"fetching {LUA_URL}")
        with urllib.request.urlopen(LUA_URL) as r:
            dest.write_bytes(r.read())
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != LUA_SHA256:
        sys.exit(f"{dest}: sha256 mismatch ({digest})")
    return dest


def sub_once(pattern: str, repl: str, text: str, flags: int = 0) -> str:
    out, n = re.subn(pattern, lambda _: repl, text, count=1, flags=flags)
    if n != 1:
        raise RuntimeError(f"pattern not found: {pattern}")
    return out


def patch(src: Path) -> int:
    """Renumber opcodes in a stock Lua 5.4 `src/` tree. Returns the opcode count."""

    def edit(name: str, fn) -> None:
        path = src / name
        path.write_text(fn(path.read_text()))

    header = (src / "lopcodes.h").read_text()
    enum_body = re.search(r"typedef enum \{(.*?)\} OpCode;", header, re.S)[1]
    stock = re.findall(r"^OP_(\w+)", enum_body, re.M)
    index = {name: i for i, name in enumerate(stock)}

    # (name, stock base) in game order
    table: list[tuple[str, str]] = []
    for name in stock:
        table.append((name, name))
        table.extend(INSERTIONS.get(name, []))
    assert table[-1][0] == "EXTRAARG"  # NUM_OPCODES is derived from it
    assert len(table) <= 128  # 7-bit opcode field

    enum = "typedef enum {\n" + ",\n".join(f"OP_{n}" for n, _ in table) + "\n} OpCode;"
    edit("lopcodes.h", lambda t: sub_once(r"typedef enum \{.*?\} OpCode;", enum, t, re.S))

    def opmodes(text: str) -> str:
        modes = re.findall(r"^\s*,?\s*(opmode\([^)]*\))", text, re.M)
        assert len(modes) == len(stock), len(modes)
        body = "\n".join(
            ("  " if i == 0 else " ,") + modes[index[base]] + f"\t\t/* OP_{name} */"
            for i, (name, base) in enumerate(table)
        )
        m = re.search(r"(luaP_opmodes\[[^\]]*\]\s*=\s*\{)(.*?)(\n\};)", text, re.S)
        return text[: m.end(1)] + "\n" + body + text[m.start(3) :]

    edit("lopcodes.c", opmodes)

    def opnames(text: str) -> str:
        body = "\n".join(f'  "{n}",' for n, _ in table) + "\n  NULL"
        m = re.search(r"(opnames\[\]\s*=\s*\{)(.*?)(\n\};)", text, re.S)
        return text[: m.end(1)] + "\n" + body + text[m.start(3) :]

    edit("lopnames.h", opnames)

    labels = "\n".join(f"&&L_OP_{n}," for n, _ in table) + "\n"
    edit("ljumptab.h", lambda t: sub_once(r"(?:&&L_OP_\w+,\s*)+", labels, t))

    def vm(text: str) -> str:
        # inserted opcodes share the base opcode's body via stacked case labels
        aliases: dict[str, list[str]] = {}
        for name, base in table:
            if name != base:
                aliases.setdefault(base, []).append(name)
        for base, extra in aliases.items():
            case = f"vmcase(OP_{base})"
            assert text.count(case) == 1, case
            stacked = " ".join(f"vmcase(OP_{e})" for e in extra)
            text = text.replace(case, f"{stacked}\n      {case}")
        return text

    edit("lvm.c", vm)
    return len(table)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("."), help="where to put the binaries")
    ap.add_argument("--work", type=Path, default=Path("build"), help="download/build dir")
    ap.add_argument("--plat", default="guess", help="Lua make target (linux, macosx, mingw, ...)")
    ap.add_argument("--suffix", default="_custom", help="appended to lua/luac binary names")
    args = ap.parse_args()

    args.work.mkdir(parents=True, exist_ok=True)
    tarball = fetch(args.work / f"lua-{LUA_VERSION}.tar.gz")
    tree = args.work / f"lua-{LUA_VERSION}"
    shutil.rmtree(tree, ignore_errors=True)
    with tarfile.open(tarball) as tf:
        tf.extractall(args.work, filter="data")

    print(f"patched: {patch(tree / 'src')} opcodes")
    subprocess.run(["make", "-C", str(tree), args.plat], check=True, stdout=subprocess.DEVNULL)

    args.out.mkdir(parents=True, exist_ok=True)
    for name in ("lua", "luac"):
        built = next(p for p in (tree / "src" / name, tree / "src" / f"{name}.exe") if p.exists())
        dest = args.out / (name + args.suffix + built.suffix)
        shutil.copy2(built, dest)
        print(f"built: {dest}")


if __name__ == "__main__":
    main()
