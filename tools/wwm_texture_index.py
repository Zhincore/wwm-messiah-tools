#!/usr/bin/env python3
"""
Index texture containers without extracting them: read each entry's header and record
file id, container, offset, size, pixel format, dimensions and mip count.

Usage:
  wwm_texture_index.py <install_root> -o textures.csv [-j N]
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import multiprocessing as mp
import struct
import sys
from collections import defaultdict
from pathlib import Path

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


def scan_pack(args):
    container, entries = args
    rows = []
    try:
        pack = wt.Pack(container, entries)
    except OSError:
        return container, rows
    if pack.blocks is not None:
        items = [(e, pack.block_data(b)[:16]) for b, e in pack.match_blocks(entries) if e is not None]
    else:
        items = ((e, pack.read(e.offset, 16)) for e in sorted(entries, key=lambda e: e.offset) if e.offset + e.size <= pack.size)
    name = Path(container).name
    for e, head in items:
        if head[:4] != wt.TEX_MAGIC or len(head) < 16:
            continue
        fmt, mips = head[5], head[6]
        w, h = struct.unpack_from("<HH", head, 12)
        rows.append((f"{e.file_id:016x}", name, e.offset, e.size, fmt, w, h, mips, head[7]))
    return container, rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-j", "--jobs", type=int, default=3)
    args = ap.parse_args()
    entries = wt.collect_entries(Path(args.install_root), {"res", "lt", "cache", "db"})
    by: dict[str, list] = defaultdict(list)
    for e in entries:
        by[e.container].append(e)
    jobs = sorted(by.items())
    print(f"{len(entries)} entries in {len(jobs)} containers", flush=True)
    n = 0
    with open(args.output, "w", newline="") as f, mp.get_context("spawn").Pool(args.jobs) as pool:
        w = csv.writer(f)
        w.writerow(["file_id", "container", "offset", "size", "format", "width", "height", "mips", "flags"])
        for i, (container, rows) in enumerate(pool.imap_unordered(scan_pack, jobs), 1):
            w.writerows(rows)
            f.flush()
            n += len(rows)
            if i % 20 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] {n} textures", flush=True)
    print(f"wrote {n} texture rows to {args.output}")


if __name__ == "__main__":
    main()
