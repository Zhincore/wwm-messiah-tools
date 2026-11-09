#!/usr/bin/env python3
"""
Harvest cleartext asset paths from pack payloads and match them to index file ids.

Serialized UI/scene objects reference other assets by path ("UIScript/x.csb",
"UI/.../foo.png", "*.plist"). The index key is file_id = (MurmurHash3_x86_32(path) << 32)
| TailSig(path without extension), so every harvested path can be hashed and joined
against the manifest, which names otherwise anonymous textures.

Usage:
  wwm_harvest_paths.py <install_root> -o paths.txt [-j N] [--max-size BYTES] [--save-csb DIR]
  wwm_harvest_paths.py --match paths.txt --manifest manifest.csv -o names.csv
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import multiprocessing as mp
import os
import re
import struct
import sys
from collections import defaultdict
from pathlib import Path

_here = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("wwm_textures", _here / "wwm_textures.py")
wt = importlib.util.module_from_spec(_spec)
sys.modules["wwm_textures"] = wt
_spec.loader.exec_module(wt)

PATH_RE = re.compile(rb"[A-Za-z0-9_][A-Za-z0-9_\-./@ ]{3,200}\.[a-z0-9]{1,6}")
EXTS = {b"png", b"plist", b"csb", b"json", b"lua", b"ttf", b"fnt", b"atlas", b"skel", b"mp4", b"ogg",
        b"wav", b"bnk", b"wem", b"jpg", b"astc", b"pvr", b"ktx", b"dds", b"tga", b"xml", b"csv", b"txt",
        b"prefab", b"mesh", b"skin", b"anim", b"mat", b"fx", b"shader", b"scene", b"bin", b"dat", b"pb"}


# --------------------------------------------------------------------------
# file_id (docs: ArArgon/yysls-re file-id.md)
# --------------------------------------------------------------------------


def murmur3_32(data: bytes, seed: int = 0) -> int:
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed
    n = len(data) // 4 * 4
    for i in range(0, n, 4):
        k = struct.unpack_from("<I", data, i)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    k = 0
    tail = data[n:]
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= len(data)
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def strip_ext(p: bytes) -> bytes:
    dot, slash = p.rfind(b"."), p.rfind(b"/")
    return p[:dot] if dot > slash else p


def tail_sig(b: bytes) -> int:
    n = len(b)
    if n == 0:
        return 0
    if n == 1:
        return (b[0] << 24) | n
    if n == 2:
        return (b[1] << 24) | (b[0] << 16) | n
    return (b[n - 1] << 24) | (b[n - 2] << 16) | (b[n - 3] << 8) | (n & 0xFF)


def file_id(path: str) -> int:
    p = path.encode("utf-8")
    stripped = strip_ext(p)
    if len(stripped) in (39, 41) and len(stripped) > 2 and stripped[2:3] == b"/":
        lo = struct.unpack(">I", stripped[3:7])[0]
    else:
        lo = tail_sig(stripped)
    return (murmur3_32(p) << 32) | lo


# --------------------------------------------------------------------------
# Harvest
# --------------------------------------------------------------------------


def looks_like_csb(buf: bytes) -> bool:
    """FlatBuffer whose root table is CSParseBinary: version string "1.0" reachable at field 0."""
    if len(buf) < 32:
        return False
    try:
        root = struct.unpack_from("<I", buf, 0)[0]
        vt = root - struct.unpack_from("<i", buf, root)[0]
        vsize = struct.unpack_from("<H", buf, vt)[0]
        if vsize < 16:
            return False
        off = struct.unpack_from("<H", buf, vt + 4)[0]
        sp = root + off
        sp += struct.unpack_from("<I", buf, sp)[0]
        n = struct.unpack_from("<I", buf, sp)[0]
        return 1 <= n <= 8 and buf[sp + 4 : sp + 4 + n] in (b"1.0", b"2.0", b"1.1", b"2.1")
    except (struct.error, IndexError):
        return False


def harvest_pack(args: tuple[str, list, int, str | None]) -> tuple[set[bytes], int]:
    container, entries, max_size, save_dir = args
    found: set[bytes] = set()
    saved = 0
    try:
        pack = wt.Pack(container, entries)
    except OSError:
        return found, saved
    if pack.blocks is not None:
        def blocks():
            for b in pack.blocks:
                try:
                    yield None, pack.block_data(b)
                except Exception:
                    continue
        payloads = blocks()
    else:
        def gen():
            for e in sorted(entries, key=lambda e: e.offset):
                if e.size > max_size or e.offset + e.size > pack.size:
                    continue
                yield e, pack.read(e.offset, e.size)
        payloads = gen()
    for entry, buf in payloads:
        if buf[:4] in (b"EZST", b"ZSTD"):
            try:
                buf = wt.ezst_decode(buf)
            except Exception:
                continue
        if buf[:4] == wt.TEX_MAGIC or buf[:4] in (b"RIFF", b"BKHD", b"ZZZ4", b"LZMA", b".MES"):
            continue
        if save_dir and entry is not None and looks_like_csb(buf):
            Path(save_dir, f"{entry.file_id:016x}.csb").write_bytes(buf)
            saved += 1
        for i, m in enumerate(PATH_RE.finditer(buf)):
            if i > 20000:
                break
            s = m.group(0)
            if s.rsplit(b".", 1)[-1].lower() in EXTS:
                found.add(s.strip())
    return found, saved


def cmd_harvest(args: argparse.Namespace) -> None:
    root = Path(args.install_root)
    entries = wt.collect_entries(root, {"res", "lt", "cache", "db"})
    by_container: dict[str, list] = defaultdict(list)
    for e in entries:
        by_container[e.container].append(e)
    if args.save_csb:
        Path(args.save_csb).mkdir(parents=True, exist_ok=True)
    jobs = [(c, es, args.max_size, args.save_csb) for c, es in sorted(by_container.items())]
    print(f"{len(entries)} entries in {len(jobs)} containers", flush=True)
    found: set[bytes] = set()
    saved = 0
    # spawn: workers must not inherit the parent's full index (copy-on-write blow-up)
    with mp.get_context("spawn").Pool(args.jobs, maxtasksperchild=8) as pool:
        for i, (s, n) in enumerate(pool.imap_unordered(harvest_pack, jobs), 1):
            found |= s
            saved += n
            if i % 20 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] {len(found)} unique paths, {saved} csb saved", flush=True)
    with open(args.output, "wb") as f:
        for s in sorted(found):
            f.write(s + b"\n")
    print(f"wrote {len(found)} paths to {args.output}")


def cmd_match(args: argparse.Namespace) -> None:
    paths = [l.rstrip(b"\n") for l in open(args.match, "rb") if l.strip()]
    ids: dict[int, list[str]] = defaultdict(list)
    for p in paths:
        try:
            ids[file_id(p.decode("utf-8"))].append(p.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    rows = list(csv.DictReader(open(args.manifest, newline="")))
    hit = 0
    with open(args.output, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file_id", "path", "status", "format", "width", "height", "file"])
        for r in rows:
            names = ids.get(int(r["file_id"], 16))
            if names:
                hit += 1
                w.writerow([r["file_id"], names[0], r["status"], r["format"], r["width"], r["height"], r["path"]])
    print(f"{len(paths)} paths -> {len(ids)} ids; matched {hit}/{len(rows)} manifest rows -> {args.output}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root", nargs="?")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--max-size", type=int, default=512_000, help="skip payloads larger than this")
    ap.add_argument("--save-csb", metavar="DIR", help="also save every Cocos .csb payload here as <file_id>.csb")
    ap.add_argument("--match", metavar="PATHS", help="match harvested paths against a manifest instead")
    ap.add_argument("--manifest")
    args = ap.parse_args()
    if args.match:
        cmd_match(args)
    else:
        if not args.install_root:
            ap.error("install_root required for harvesting")
        cmd_harvest(args)


if __name__ == "__main__":
    main()
