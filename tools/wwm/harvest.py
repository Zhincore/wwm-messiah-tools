#!/usr/bin/env python3
"""
Harvest cleartext asset paths from pack payloads and match them to index file ids.

Serialized UI/scene objects reference other assets by path ("UIScript/x.csb",
"UI/.../foo.png", "*.plist"). The index key is file_id = (MurmurHash3_x86_32(path) << 32)
| TailSig(path without extension), so every harvested path can be hashed and joined
against the manifest, which names otherwise anonymous textures.

Usage:
  wwm-harvest <install_root> -o paths.txt [-j N] [--max-size BYTES] [--save-csb DIR]
  wwm-harvest --match paths.txt --manifest manifest.csv -o names.csv
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import os
import re
import struct
from collections import defaultdict
from pathlib import Path

from . import codecs, vfs
from .ids import file_id



PATH_RE = re.compile(rb"[A-Za-z0-9_][A-Za-z0-9_\-./@ ]{3,200}\.[a-z0-9]{1,6}")
EXTS = {b"png", b"plist", b"csb", b"json", b"lua", b"ttf", b"fnt", b"atlas", b"skel", b"mp4", b"ogg",
        b"wav", b"bnk", b"wem", b"jpg", b"astc", b"pvr", b"ktx", b"dds", b"tga", b"xml", b"csv", b"txt",
        b"prefab", b"mesh", b"skin", b"anim", b"mat", b"fx", b"shader", b"scene", b"bin", b"dat", b"pb"}


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


def harvest_pack(args: tuple[str, list, int, str | None]) -> tuple[str, set[bytes], int]:
    container, entries, max_size, save_dir = args
    found: set[bytes] = set()
    saved = 0
    try:
        pack = vfs.Pack(container, entries)
    except OSError:
        return container, found, saved
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
                buf = codecs.ezst_decode(buf)
            except Exception:
                continue
        if buf[:4] == vfs.TEX_MAGIC or buf[:4] in (b"RIFF", b"BKHD", b"ZZZ4", b"LZMA", b".MES"):
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
    return container, found, saved


def cmd_harvest(args: argparse.Namespace) -> None:
    root = Path(args.install_root)
    entries = vfs.collect_entries(root, {"res", "lt", "cache", "db"})
    by_container: dict[str, list] = defaultdict(list)
    for e in entries:
        by_container[e.container].append(e)
    if args.save_csb:
        Path(args.save_csb).mkdir(parents=True, exist_ok=True)
    done_path = Path(args.output + ".done")
    done = set(done_path.read_text().split()) if args.resume and done_path.exists() else set()
    jobs = [(c, es, args.max_size, args.save_csb) for c, es in sorted(by_container.items())
            if Path(c).name not in done]
    print(f"{len(entries)} entries in {len(by_container)} containers, {len(jobs)} to do", flush=True)
    found: set[bytes] = set()
    if args.resume and Path(args.output).exists():
        found = {l.rstrip(b"\n") for l in open(args.output, "rb") if l.strip()}
    saved = 0
    # Paths are appended per pack and the pack recorded as done, so a killed run resumes.
    out = open(args.output, "ab" if args.resume else "wb")
    done_file = open(done_path, "a" if args.resume else "w")
    # spawn: workers must not inherit the parent's full index (copy-on-write blow-up)
    with mp.get_context("spawn").Pool(args.jobs, maxtasksperchild=8) as pool:
        for i, (container, s, n) in enumerate(pool.imap_unordered(harvest_pack, jobs), 1):
            new = s - found
            found |= new
            saved += n
            out.write(b"".join(p + b"\n" for p in sorted(new)))
            out.flush()
            done_file.write(Path(container).name + "\n")
            done_file.flush()
            if i % 20 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] {len(found)} unique paths, {saved} csb saved", flush=True)
    out.close()
    done_file.close()
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
    ap.add_argument("--resume", action="store_true", help="skip packs listed in <output>.done and append")
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
