#!/usr/bin/env python3
"""
Dump the game's Lua scripts: LuaText wrapper -> stock Lua 5.4 bytecode -> (optional) source.

  on disk   u32le decoded_size + raw LZ4 block                (LuaText, see yysls-re lua-bytecode.md)
  decoded   stock Lua 5.4 chunk with NetEase's extended opcode table
  remapped  opcodes rewritten to stock numbering (table from ArArgon/yysls-re, messiah.lua.luac)
  source    `java -jar unluac.jar` on the remapped chunk, when a jar is given

Script entries are keyed by path hash like everything else; harvested paths resolve with the
"LuaText/" prefix. Entries without a recovered name are written under their file id.

Usage:
  wwm_lua.py <install_root> -o OUT [--paths paths.txt] [--unluac unluac.jar] [-j N] [--limit N]
"""

from __future__ import annotations

import argparse
import importlib.util
import multiprocessing as mp
import os
import struct
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import lz4.block

_here = Path(__file__).resolve().parent


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _here / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wt = _load("wwm_textures")
hp = _load("wwm_harvest_paths")

LUA_SIGNATURE = b"\x1bLua"
CHUNK_HEADER = LUA_SIGNATURE + b"\x54\x00\x19\x93\r\n\x1a\n" + b"\x04\x08\x08" + (0x5678).to_bytes(8, "little") + struct.pack("<d", 370.5)
HEADER_LEN = len(CHUNK_HEADER)  # 31, nupvalues follows
MAX_CHUNK = 64 << 20

STD_OPCODES = [
    "MOVE", "LOADI", "LOADF", "LOADK", "LOADKX", "LOADFALSE", "LFALSESKIP", "LOADTRUE", "LOADNIL", "GETUPVAL",
    "SETUPVAL", "GETTABUP", "GETTABLE", "GETI", "GETFIELD", "SETTABUP", "SETTABLE", "SETI", "SETFIELD", "NEWTABLE",
    "SELF", "ADDI", "ADDK", "SUBK", "MULK", "MODK", "POWK", "DIVK", "IDIVK", "BANDK", "BORK", "BXORK", "SHRI", "SHLI",
    "ADD", "SUB", "MUL", "MOD", "POW", "DIV", "IDIV", "BAND", "BOR", "BXOR", "SHL", "SHR", "MMBIN", "MMBINI", "MMBINK",
    "UNM", "BNOT", "NOT", "LEN", "CONCAT", "CLOSE", "TBC", "JMP", "EQ", "LT", "LE", "EQK", "EQI", "LTI", "LEI", "GTI",
    "GEI", "TEST", "TESTSET", "CALL", "TAILCALL", "RETURN", "RETURN0", "RETURN1", "FORLOOP", "FORPREP", "TFORPREP",
    "TFORCALL", "TFORLOOP", "SETLIST", "CLOSURE", "VARARG", "VARARGPREP", "EXTRAARG",
]
STD = {n: i for i, n in enumerate(STD_OPCODES)}
# Game opcode number -> (name, stock equivalent). Inserted opcodes collapse to their generic form.
_INSERTED = {12: ("GETTABUPDOT", "GETTABUP"), 13: ("GETTABUPFAST", "GETTABUP"), 17: ("GETFIELDDOT", "GETFIELD"),
             18: ("GETFIELDFAST", "GETFIELD"), 20: ("SETTABUPDOT", "SETTABUP"), 24: ("SETFIELDDOT", "SETFIELD"),
             25: ("SETDICTPROXYUP", "SETTABUP"), 26: ("SETDICTPROXY", "SETTABLE"), 27: ("SETDICTPROXYI", "SETI"),
             28: ("SETDICTPROXYFIELD", "SETFIELD"), 30: ("NEWLIST", "NEWTABLE"), 31: ("NEWDICT", "NEWTABLE"),
             32: ("NEWLISTPROXY", "NEWTABLE"), 33: ("NEWDICTPROXY", "NEWTABLE"), 93: ("LISTSETLIST", "SETLIST")}
CUSTOM: list[tuple[str, int | None]] = []
_std_iter = iter(STD_OPCODES)
for _op in range(98):
    if _op in _INSERTED:
        CUSTOM.append((_INSERTED[_op][0], STD[_INSERTED[_op][1]]))
    else:
        _n = next(_std_iter)
        CUSTOM.append((_n, STD[_n]))
CUSTOM += [("LDAD", None), ("MMBIN_LDAD", None)]
MMBIN_OPS = {op for op, (n, _) in enumerate(CUSTOM) if n in ("MMBIN", "MMBINI", "MMBINK", "MMBIN_LDAD")}


class ChunkError(ValueError):
    pass


def read_varint(buf, pos):
    x = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        x = (x << 7) | (b & 0x7F)
        if b & 0x80:
            return x, pos
    raise ChunkError("truncated varint")


def skip_string(buf, pos):
    n, pos = read_varint(buf, pos)
    return pos + n - 1 if n else pos


def remap(instr: int) -> tuple[int, bool]:
    op = instr & 0x7F
    if op >= len(CUSTOM) or CUSTOM[op][1] is None:
        return instr, False
    out = (instr & ~0x7F) | CUSTOM[op][1]
    if op in MMBIN_OPS:
        c = (out >> 24) & 0xFF
        if c < len(CUSTOM) and CUSTOM[c][1] is not None:
            out = (out & 0x00FFFFFF) | (CUSTOM[c][1] << 24)
    return out, True


def _proto(buf: bytearray, pos: int, warnings: list[str]) -> int:
    pos = skip_string(buf, pos)
    _, pos = read_varint(buf, pos)
    _, pos = read_varint(buf, pos)
    pos += 3
    n, pos = read_varint(buf, pos)
    for i in range(n):
        instr = struct.unpack_from("<I", buf, pos)[0]
        new, ok = remap(instr)
        if not ok:
            warnings.append(f"unmapped opcode {instr & 0x7F} at code[{i}]")
        struct.pack_into("<I", buf, pos, new)
        pos += 4
    n, pos = read_varint(buf, pos)
    for _ in range(n):
        tag = buf[pos]
        pos += 1
        if tag in (0x03, 0x13):
            pos += 8
        elif tag in (0x04, 0x14):
            pos = skip_string(buf, pos)
        elif tag not in (0x00, 0x01, 0x11):
            raise ChunkError(f"unknown constant tag {tag:#x}")
    n, pos = read_varint(buf, pos)
    pos += 3 * n
    n, pos = read_varint(buf, pos)
    for _ in range(n):
        pos = _proto(buf, pos, warnings)
    n, pos = read_varint(buf, pos)
    pos += n
    n, pos = read_varint(buf, pos)
    for _ in range(n):
        _, pos = read_varint(buf, pos)
        _, pos = read_varint(buf, pos)
    n, pos = read_varint(buf, pos)
    for _ in range(n):
        pos = skip_string(buf, pos)
        _, pos = read_varint(buf, pos)
        _, pos = read_varint(buf, pos)
    n, pos = read_varint(buf, pos)
    for _ in range(n):
        pos = skip_string(buf, pos)
    return pos


def unpack_luatext(blob: bytes) -> bytes | None:
    """LuaText -> chunk, or None when the blob is not one."""
    if len(blob) < 8:
        return None
    size = struct.unpack_from("<I", blob, 0)[0]
    if not HEADER_LEN < size <= MAX_CHUNK:
        return None
    try:
        out = lz4.block.decompress(blob[4:], uncompressed_size=size)
    except Exception:
        return None
    return out if out[:HEADER_LEN] == CHUNK_HEADER else None


def remap_chunk(chunk: bytes) -> tuple[bytes, list[str]]:
    buf = bytearray(chunk)
    warnings: list[str] = []
    _proto(buf, HEADER_LEN + 1, warnings)
    return bytes(buf), warnings


def chunk_source_name(chunk: bytes) -> str | None:
    """The `source` string of the main prototype ("@path/file.lua"), if present."""
    try:
        n, pos = read_varint(chunk, HEADER_LEN + 1)
        if n > 1:
            return chunk[pos : pos + n - 1].decode("utf-8", errors="replace")
    except ChunkError:
        pass
    return None


# --------------------------------------------------------------------------


def process_pack(args) -> tuple[str, list[tuple[int, str, str, int]], Counter]:
    container, entries, out_dir, names, limit = args
    out_dir = Path(out_dir)
    rows, stats = [], Counter()
    try:
        pack = wt.Pack(container, entries)
    except OSError:
        return container, rows, stats
    if pack.blocks is not None:
        items = [(e, b) for b, e in pack.match_blocks(entries)]
    else:
        items = [(e, None) for e in sorted(entries, key=lambda e: e.offset) if e.offset + e.size <= pack.size]
    done = 0
    for entry, block in items:
        if limit and done >= limit:
            break
        if entry is None:
            continue
        try:
            blob = pack.block_data(block) if block else pack.read(entry.offset, min(entry.size, MAX_CHUNK))
        except Exception:
            continue
        if blob[:4] in (b"EZST", b"ZSTD"):
            try:
                blob = wt.ezst_decode(blob)
            except Exception:
                continue
        chunk = unpack_luatext(blob)
        if chunk is None:
            continue
        stats["lua"] += 1
        try:
            fixed, warnings = remap_chunk(chunk)
        except ChunkError as e:
            stats["remap_failed"] += 1
            rows.append((entry.file_id, "", f"remap failed: {e}", len(chunk)))
            continue
        stats["unmapped"] += len(warnings)
        name = names.get(entry.file_id) or chunk_source_name(chunk) or f"{entry.file_id:016x}.lua"
        name = name.lstrip("@=").replace("\\", "/").lstrip("/")
        if name.startswith("LuaText/"):
            name = name[len("LuaText/"):]
        target = out_dir / (name if name.endswith(".lua") else name + ".lua")
        target = target.with_suffix(".luac")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fixed)
        rows.append((entry.file_id, str(target.relative_to(out_dir)), "; ".join(warnings[:3]), len(chunk)))
        done += 1
    return container, rows, stats


def decompile_all(out_dir: Path, jar: str, jobs: int) -> Counter:
    files = sorted(out_dir.rglob("*.luac"))
    stats = Counter()

    def run(f: Path):
        target = f.with_suffix(".lua")
        if target.exists():
            return "skipped"
        res = subprocess.run(["java", "-jar", jar, str(f)], capture_output=True, text=True, errors="replace")
        if res.returncode == 0 and res.stdout.strip():
            target.write_text(res.stdout, encoding="utf-8")
            return "decompiled"
        f.with_suffix(".unluac.err").write_text(res.stderr[-2000:], encoding="utf-8")
        return "decompile_failed"

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(jobs) as ex:
        for i, outcome in enumerate(ex.map(run, files), 1):
            stats[outcome] += 1
            if i % 2000 == 0:
                print(f"[{i}/{len(files)}] {dict(stats)}", flush=True)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root", nargs="?", default=".")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--paths", help="harvested path list for naming (wwm_harvest_paths.py)")
    ap.add_argument("--unluac", metavar="JAR", help="decompile with unluac after extraction")
    ap.add_argument("-j", "--jobs", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="max scripts per container (smoke test)")
    ap.add_argument("--container", metavar="REGEX")
    ap.add_argument("--decompile-only", action="store_true", help="skip extraction; decompile .luac already in -o")
    args = ap.parse_args()
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.decompile_only:
        if not args.unluac:
            ap.error("--decompile-only needs --unluac JAR")
        print("decompiling existing .luac files...", flush=True)
        print("done:", dict(decompile_all(out_dir, args.unluac, args.jobs)))
        return

    root = Path(args.install_root)
    entries = wt.collect_entries(root, {"res", "lt", "cache", "db"})
    ids = {e.file_id for e in entries}
    names: dict[int, str] = {}
    if args.paths:
        for line in open(args.paths, "rb"):
            p = line.strip().decode("utf-8", errors="ignore")
            if not p.endswith(".lua"):
                continue
            for cand in (p, "LuaText/" + p):
                fid = hp.file_id(cand)
                if fid in ids:
                    names.setdefault(fid, p)
                    break
        print(f"{len(names)} script entries named from {args.paths}", flush=True)

    import re
    by_container: dict[str, list] = defaultdict(list)
    for e in entries:
        if args.container and not re.search(args.container, Path(e.container).name):
            continue
        by_container[e.container].append(e)
    jobs = []
    for c, es in sorted(by_container.items()):
        local = {e.file_id: names[e.file_id] for e in es if e.file_id in names}
        jobs.append((c, es, str(out_dir), local, args.limit))
    print(f"{len(entries)} entries in {len(jobs)} containers -> {out_dir}", flush=True)

    total = Counter()
    with open(out_dir / "scripts.csv", "w") as man, mp.get_context("spawn").Pool(args.jobs) as pool:
        man.write("file_id,path,warnings,chunk_size\n")
        for i, (container, rows, stats) in enumerate(pool.imap_unordered(process_pack, jobs), 1):
            total.update(stats)
            for fid, path, warn, size in rows:
                man.write(f"{fid:016x},{path},\"{warn}\",{size}\n")
            man.flush()
            if i % 20 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] {dict(total)}", flush=True)
    if args.unluac:
        print("decompiling...", flush=True)
        total.update(decompile_all(out_dir, args.unluac, max(2, os.cpu_count() // 2)))
    print("done:", dict(total))


if __name__ == "__main__":
    main()
