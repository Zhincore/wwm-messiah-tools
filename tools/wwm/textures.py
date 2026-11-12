#!/usr/bin/env python3
"""
Extract textures and loose images from a Where Winds Meet (Messiah engine) install.

Index sources (all resolved by filename convention, never by hardcoded pack numbers):
  Resources.mpkinfo            -> Resources.mpk            (install root)
  LocalData/Patch/LT{n}.mpkinfo -> LT{n}.mpk               (patch root)
  LocalData/Patch/*.mpkinfo     -> Patch{pack_id}.mpk      (per entry; MpkCached_*, TinyFiles_*, Patch{n})
  LocalData/Patch/Patch.mpkdb   -> Patch{v2}.mpk           (SQLite, tables info0 + info1)

Payload wrappers:
  EZST  "EZST" u32 raw_size, payload: first min(floor16(len),256) bytes AES-256-ECB, then one zstd frame.
        Key and chunked-pack model from ArArgon/yysls-re (messiah.codec.ezst).
  Texture container  magic 02 02 02 01, byte5 = EPixelFormat, byte6 = mip count, u16 w/h at 0x0c,
        u32 total at 0x20, u32 tag at 0x24, then mip records smallest-first:
        u32 blob, u16 w, u16 h, u16 depth, u16 pitch, u32 raw, EZST block (payload = blob - 24); 16-byte trailer.

Usage:
  wwm-textures scan    <install_root>
  wwm-textures extract <install_root> -o <out_dir> [--limit N] [--sources res,lt,cache,db] [--dds] [-j N]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import multiprocessing as mp
import os
import re
import struct
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path


from .codecs import ezst_decode
from .vfs import TEX_MAGIC, Block, Entry, Pack, collect_entries


# EPixelFormat (texture/src/pixel_format.rs) -> (name, DXGI format, bytes per 4x4 block or per pixel, is_block)
PIXEL_FORMATS: dict[int, tuple[str, int | None, float, bool]] = {
    1: ("A32R32G32B32F", 2, 16, False),
    2: ("A16B16G16R16F", 10, 8, False),
    3: ("R8G8B8A8", 28, 4, False),
    4: ("B5G6R5", 85, 2, False),
    5: ("A8L8", None, 2, False),
    6: ("G16R16", 35, 4, False),
    7: ("G16R16F", 34, 4, False),
    8: ("G32R32F", 16, 8, False),
    9: ("R32F", 41, 4, False),
    10: ("R16F", 54, 2, False),
    11: ("L8", 61, 1, False),
    12: ("L16", 56, 2, False),
    13: ("A8", 65, 1, False),
    18: ("BC1", 71, 8, True),
    19: ("BC2", 74, 16, True),
    20: ("BC3", 77, 16, True),
    21: ("BC4", 80, 8, True),
    22: ("BC5", 83, 16, True),
    23: ("BC6H_SF", 96, 16, True),
    24: ("BC6H_UF", 95, 16, True),
    25: ("BC7", 98, 16, True),
    52: ("R10G10B10A2", 24, 4, False),
    53: ("R32U", 42, 4, False),
    54: ("R11G11B10F", 26, 4, False),
}

IMAGE_MAGICS = [
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"DDS ", "dds"),
    (b"RIFF", "webp"),  # verified below with WEBP tag
    (b"BM", "bmp"),
    (b"GIF8", "gif"),
    (b"KTX ", "ktx"),
]


# --------------------------------------------------------------------------
# Texture container
# --------------------------------------------------------------------------


@dataclass
class Mip:
    w: int
    h: int
    depth: int
    pitch: int
    raw: int
    data: bytes


@dataclass
class Texture:
    fmt: int
    mip_count: int
    width: int
    height: int
    flags: bytes  # bytes 7..12 of the header, kept for the manifest
    mips: list[Mip]
    warnings: list[str]


def parse_texture(buf: bytes) -> Texture:
    if buf[:4] != TEX_MAGIC or len(buf) < 0x28:
        raise ValueError("not a texture container")
    fmt, mip_count = buf[5], buf[6]
    width, height = struct.unpack_from("<HH", buf, 0x0C)
    warnings: list[str] = []
    mips: list[Mip] = []
    pos = 0x28
    while pos + 16 <= len(buf):
        blob, w, h, depth, pitch, raw = struct.unpack_from("<IHHHHI", buf, pos)
        pos += 16
        if buf[pos : pos + 4] not in (b"EZST", b"ZSTD"):
            warnings.append(f"no EZST at {pos}")
            break
        payload_len = max(0, blob - 24)
        block = buf[pos : pos + 8 + payload_len]
        if raw == 0:
            data = b""
        else:
            try:
                data = ezst_decode(block, verify=False)
            except Exception as e:
                warnings.append(f"mip {w}x{h}: {e}")
                data = b""
            if len(data) != raw:
                warnings.append(f"mip {w}x{h}: got {len(data)} want {raw}")
        mips.append(Mip(w, h, depth, pitch, raw, data))
        pos += 8 + payload_len
        if pos + 16 == len(buf):  # 16-byte trailer
            pos = len(buf)
            break
    if pos != len(buf):
        warnings.append(f"ended at {pos}/{len(buf)}")
    return Texture(fmt, mip_count, width, height, buf[7:12], mips, warnings)


def dds_bytes(w: int, h: int, dxgi: int, data: bytes) -> bytes:
    header = struct.pack("<4sIIIIIII", b"DDS ", 124, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000, h, w, len(data), 0, 0)
    header += b"\0" * 44
    header += struct.pack("<II4sIIIII", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
    header += struct.pack("<IIIII", 0x1000, 0, 0, 0, 0)
    header += struct.pack("<IIIII", dxgi, 3, 0, 1, 0)
    return header + data


def to_png(fmt: int, w: int, h: int, data: bytes) -> bytes | None:
    """Best-effort PNG conversion via Pillow. None when the format is not decodable."""
    from PIL import Image

    try:
        if fmt == 3:
            im = Image.frombytes("RGBA", (w, h), data)
        elif fmt == 11:
            im = Image.frombytes("L", (w, h), data)
        elif fmt == 5:
            im = Image.frombytes("LA", (w, h), data)
        else:
            info = PIXEL_FORMATS.get(fmt)
            if not info or info[1] is None:
                return None
            im = Image.open(io.BytesIO(dds_bytes(w, h, info[1], data)))
            im.load()
        out = io.BytesIO()
        im.save(out, "PNG")
        return out.getvalue()
    except Exception:
        return None


# --------------------------------------------------------------------------
# Classification and extraction
# --------------------------------------------------------------------------


def classify(buf: bytes) -> str:
    if buf[:4] == TEX_MAGIC:
        return "texture"
    if buf[:4] in (b"EZST", b"ZSTD"):
        return "ezst"
    for magic, ext in IMAGE_MAGICS:
        if buf.startswith(magic):
            if ext == "webp" and buf[8:12] != b"WEBP":
                continue
            return ext
    return "other"


@dataclass
class Result:
    entry: Entry
    kind: str
    status: str
    out_path: str = ""
    md5: str = ""
    fmt: str = ""
    width: int = 0
    height: int = 0
    mips: int = 0
    note: str = ""


def _group_top_mips(tex: Texture) -> list[Mip]:
    """Largest mip level; several records at that size mean array/cube layers."""
    valid = [m for m in tex.mips if m.data]
    if not valid:
        return []
    top = max(m.w * m.h for m in valid)
    return [m for m in valid if m.w * m.h == top]


def extract_texture(entry: Entry, buf: bytes, out_dir: Path, keep_dds: bool) -> list[Result]:
    tex = parse_texture(buf)
    info = PIXEL_FORMATS.get(tex.fmt)
    fmt_name = info[0] if info else f"fmt{tex.fmt}"
    base = f"{entry.file_id:016x}"
    tops = _group_top_mips(tex)
    note = "; ".join(tex.warnings)
    if not tops:
        return [Result(entry, "texture", "no_payload", fmt=fmt_name, width=tex.width, height=tex.height,
                       mips=tex.mip_count, note=note or f"flags={tex.flags.hex()}")]
    results = []
    for i, mip in enumerate(tops):
        suffix = f"_L{i}" if len(tops) > 1 else ""
        md5 = hashlib.md5(mip.data).hexdigest()
        png = to_png(tex.fmt, mip.w, mip.h, mip.data)
        target_dir = out_dir / entry.source
        target_dir.mkdir(parents=True, exist_ok=True)
        if png is not None:
            path = target_dir / f"{base}{suffix}.png"
            path.write_bytes(png)
            status = "png"
        else:
            status = "dds_only" if info and info[1] else "raw_only"
            path = None
        if keep_dds or png is None:
            if info and info[1] is not None:
                dpath = target_dir / f"{base}{suffix}.dds"
                dpath.write_bytes(dds_bytes(mip.w, mip.h, info[1], mip.data))
            else:
                dpath = target_dir / f"{base}{suffix}.{fmt_name}.bin"
                dpath.write_bytes(mip.data)
            path = path or dpath
        results.append(Result(entry, "texture", status, str(path), md5, fmt_name, mip.w, mip.h,
                              tex.mip_count, note or f"flags={tex.flags.hex()}"))
    return results


def process_pack(args: tuple[str, list[Entry], str, bool, int]) -> tuple[str, list[Result], Counter]:
    container, entries, out_dir_s, keep_dds, limit = args
    out_dir = Path(out_dir_s)
    results: list[Result] = []
    kinds: Counter = Counter()
    try:
        pack = Pack(container, entries)
    except OSError as e:
        return container, [Result(e_, "pack", "open_failed", note=str(e)) for e_ in entries], kinds
    if pack.blocks is not None:
        items = []
        for block, entry in pack.match_blocks(entries):
            if entry is None:
                entry = Entry(0, block.phys_off, block.phys_size, container, entries[0].source,
                              f"block@{block.phys_off}")
            items.append((entry, None, block))
        kinds["unmatched_blocks"] = sum(1 for e, _, _ in items if e.file_id == 0)
    else:
        items = []
        for entry in sorted(entries, key=lambda e: e.offset):
            if entry.offset + entry.size > pack.size:
                results.append(Result(entry, "oob", "out_of_bounds", note=f"extent={pack.size}"))
                continue
            items.append((entry, None, "entry"))

    done = 0
    for entry, buf, origin in items:
        if limit and done >= limit:
            break
        if isinstance(origin, Block):
            try:
                buf = pack.block_data(origin)
            except Exception as e:
                results.append(Result(entry, "ezst", "decode_failed", note=str(e)))
                continue
        head = buf[:64] if buf is not None else pack.read(entry.offset, min(entry.size, 64))
        kind = classify(head)
        if kind == "ezst":
            try:
                buf = ezst_decode(pack.read(entry.offset, entry.size))
                kind = "ezst:" + classify(buf)
            except Exception as e:
                kinds["ezst_fail"] += 1
                results.append(Result(entry, "ezst", "decode_failed", note=str(e)))
                continue
        kinds[kind] += 1
        inner = kind.split(":")[-1]
        if inner == "texture":
            if buf is None:
                buf = pack.read(entry.offset, entry.size)
            try:
                results += extract_texture(entry, buf, out_dir, keep_dds)
            except Exception as e:
                results.append(Result(entry, "texture", "parse_failed", note=str(e)))
            done += 1
        elif inner in dict(IMAGE_MAGICS).values():
            if buf is None:
                buf = pack.read(entry.offset, entry.size)
            target_dir = out_dir / entry.source
            target_dir.mkdir(parents=True, exist_ok=True)
            name = f"{entry.file_id:016x}" if entry.file_id else f"{Path(container).stem}_{entry.offset}"
            path = target_dir / f"{name}.{inner}"
            path.write_bytes(buf)
            results.append(Result(entry, inner, "copied", str(path), hashlib.md5(buf).hexdigest()))
            done += 1
    return container, results, kinds


def cmd_scan(args: argparse.Namespace) -> None:
    root = Path(args.install_root)
    entries = collect_entries(root, set(args.sources.split(",")))
    by_source: Counter = Counter(e.source for e in entries)
    print(f"entries: {len(entries)}  by source: {dict(by_source)}")
    by_container: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        by_container[e.container].append(e)
    print(f"containers: {len(by_container)}")
    kinds: Counter = Counter()
    for container, es in sorted(by_container.items()):
        pack = Pack(container, es)
        mx = max(e.offset + e.size for e in es)
        if pack.blocks is not None:
            matched = sum(1 for _, e in pack.match_blocks(es) if e is not None)
            for block in pack.blocks[: args.limit] if args.limit else pack.blocks:
                kinds[(es[0].source, "block:" + classify(pack.block_data(block)[:16]))] += 1
            print(f"{Path(container).name:28s} blocks   entries={len(es):7d} blocks={len(pack.blocks):6d} matched={matched:6d}")
            continue
        flag = "" if mx <= pack.size else "  !! index exceeds file"
        for e in es[: args.limit] if args.limit else es:
            if e.offset + e.size <= pack.size:
                kinds[(e.source, classify(pack.read(e.offset, min(e.size, 16))))] += 1
        print(f"{Path(container).name:28s} physical entries={len(es):7d} max={mx:12d} size={pack.size:12d}{flag}", flush=True)
    print("\nkinds by source:")
    for (src, kind), n in sorted(kinds.items()):
        print(f"  {src:6s} {kind:10s} {n}")


def cmd_extract(args: argparse.Namespace) -> None:
    root = Path(args.install_root)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = collect_entries(root, set(args.sources.split(",")))
    by_container: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        if args.container and not re.search(args.container, Path(e.container).name):
            continue
        by_container[e.container].append(e)
    jobs = [(c, es, str(out_dir), args.dds, args.limit) for c, es in sorted(by_container.items())]
    print(f"{sum(len(es) for es in by_container.values())} entries in {len(jobs)} containers -> {out_dir}", flush=True)

    manifest_path = out_dir / args.manifest
    done_path = out_dir / (args.manifest + ".done")
    seen_md5: dict[str, str] = {}
    dedup_sources = list(args.dedup_from)
    done: set[str] = set()
    if args.resume and manifest_path.exists():
        dedup_sources.append(str(manifest_path))
        if done_path.exists():
            done = set(done_path.read_text().split())
        # Containers with rows in the manifest finished, unless they were block packs
        # that an older run could not read (out_of_bounds rows).
        bad: set[str] = set()
        with open(manifest_path, newline="") as fh:
            for row in csv.DictReader(fh):
                done.add(row["container"])
                if row["status"] == "out_of_bounds":
                    bad.add(row["container"])
        done -= bad
        jobs = [j for j in jobs if Path(j[0]).name not in done]
        print(f"resume: skipping {len(done)} finished containers, {len(jobs)} left", flush=True)
    for prev in dedup_sources:
        with open(prev, newline="") as fh:
            for row in csv.DictReader(fh):
                if row["md5"] and row["path"] and row["status"] != "dup":
                    seen_md5.setdefault(row["md5"], str(out_dir / row["path"]))
    append = args.resume and manifest_path.exists()
    manifest = open(manifest_path, "a" if append else "w", newline="")
    done_file = open(done_path, "a" if append else "w")
    writer = csv.writer(manifest)
    if not append:
        writer.writerow(["file_id", "source", "index", "container", "offset", "size", "kind", "status",
                         "format", "width", "height", "mips", "path", "md5", "note"])
    totals: Counter = Counter()
    kinds_total: Counter = Counter()
    t0 = time.time()
    # spawn: workers must not inherit the parent's full index (copy-on-write blow-up)
    with mp.get_context("spawn").Pool(args.jobs) as pool:
        for i, (container, results, kinds) in enumerate(pool.imap_unordered(process_pack, jobs), 1):
            kinds_total.update(kinds)
            for r in results:
                status = r.status
                if r.md5 and r.out_path:
                    first = seen_md5.get(r.md5)
                    if first and first != r.out_path:
                        try:
                            os.remove(r.out_path)
                        except OSError:
                            pass
                        status = "dup"
                        r.note = f"dup of {os.path.relpath(first, out_dir)}"
                    else:
                        seen_md5[r.md5] = r.out_path
                totals[status] += 1
                e = r.entry
                writer.writerow([f"{e.file_id:016x}", e.source, e.index_name, Path(e.container).name, e.offset,
                                 e.size, r.kind, status, r.fmt, r.width, r.height, r.mips,
                                 os.path.relpath(r.out_path, out_dir) if r.out_path else "", r.md5, r.note])
            manifest.flush()
            done_file.write(Path(container).name + "\n")
            done_file.flush()
            if i % 10 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] {Path(container).name:24s} {dict(totals)}  {time.time() - t0:.0f}s", flush=True)
    manifest.close()
    done_file.close()
    print("\nentry kinds:", dict(kinds_total))
    print("results:", dict(totals))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="inventory indexes and entry kinds without extracting")
    s.add_argument("install_root")
    s.add_argument("--sources", default="res,lt,cache,db")
    s.add_argument("--limit", type=int, default=0, help="classify at most N entries per container")
    s.set_defaults(func=cmd_scan)
    x = sub.add_parser("extract", help="extract textures/images")
    x.add_argument("install_root")
    x.add_argument("-o", "--output", required=True)
    x.add_argument("--sources", default="res,lt,cache,db")
    x.add_argument("--limit", type=int, default=0, help="extract at most N images per container")
    x.add_argument("--dds", action="store_true", help="always write a .dds next to the .png")
    x.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4)
    x.add_argument("--container", metavar="REGEX", help="only process containers whose file name matches")
    x.add_argument("--manifest", default="manifest.csv", help="manifest file name inside the output dir")
    x.add_argument("--dedup-from", action="append", default=[], metavar="CSV",
                   help="seed content dedup from an earlier run's manifest (repeatable)")
    x.add_argument("--resume", action="store_true",
                   help="append to an existing manifest and skip containers it already covers")
    x.set_defaults(func=cmd_extract)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
