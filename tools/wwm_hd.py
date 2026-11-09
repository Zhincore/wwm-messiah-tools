#!/usr/bin/env python3
"""
Read the HD store (Package/HD/<region>/: MANIFEST, data{n}, index{n}) that holds the game's
binary data tables ("MTableReader" tables such as chat_emotion_frame_data).

Format (from ArArgon/yysls-re docs/hd-store.md, verified here):
  container  u32 magic 0xDEADBEEF, u32 version, u32 count, u32 reserved, u32 offsets[count]
             (relative to data region), blocks: u32 a=(clen<<8)|4, u32 b=(dlen<<8), u8 flags, zstd frame
  MANIFEST   same magic, zstd frame a few bytes in; payload: u16 v1, u16 v2, u32 unk0,
             u32 count_pool2, count_table, count_pool3, count_pool1; pools of (u16 len + str, 8-aligned);
             count_table records of 8 x u32: u0 = pool index, u2 = (block<<16)|data_file, u7 = pool kind
  payload    TLV (0x0a obj / 0x09 arr / 0x08 str / 0x01..0x06 ints+floats / 0x0b-0x0d null,false,true)
             or FTV (u8 field_id, u8 type, value)

Usage:
  wwm_hd.py <hd_dir> --list [REGEX]                 list manifest keys (+ record pointers)
  wwm_hd.py <hd_dir> --key NAME [-o out.json]       decode the block(s) behind a key
  wwm_hd.py <hd_dir> --block data5:123 [-o out]     decode one block directly
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import zstandard

HD_MAGIC = 0xDEADBEEF
NO_POINTER = 0xFFFFFFFF


def zstd(data: bytes) -> bytes:
    i = data.find(b"\x28\xb5\x2f\xfd")
    if i < 0:
        raise ValueError("no zstd frame")
    return zstandard.ZstdDecompressor().decompressobj().decompress(data[i:])


# ---------------------------------------------------------------- containers


def container_blocks(path: Path) -> list[tuple[int, int]]:
    data = path.read_bytes()
    magic, version, count, _ = struct.unpack_from("<IIII", data, 0)
    if magic != HD_MAGIC:
        raise ValueError(f"{path}: bad magic")
    offsets = struct.unpack_from(f"<{count}I", data, 16)
    base = 16 + 4 * count
    if offsets and offsets[-1] != len(data) - base:
        raise ValueError(f"{path}: offset table does not end at EOF")
    out, prev = [], 0
    for end in offsets:
        out.append((base + prev, base + end))
        prev = end
    return out


def read_block(path: Path, index: int) -> bytes:
    blocks = container_blocks(path)
    start, end = blocks[index]
    raw = path.read_bytes()[start:end]
    a, b = struct.unpack_from("<II", raw, 0)
    payload = zstd(raw[9:])
    if (b >> 8) and len(payload) != (b >> 8):
        raise ValueError(f"block {index}: decompressed {len(payload)} != declared {b >> 8}")
    return payload


# ---------------------------------------------------------------- manifest


@dataclass
class Manifest:
    pools: list[list[str]]
    records: list[tuple[int, ...]]

    def find(self, key: str) -> list[tuple[int, int]]:
        return [(p, pool.index(key)) for p, pool in enumerate(self.pools, 1) if key in pool]

    def pointers(self, key: str) -> list[tuple[int, int, tuple[int, ...]]]:
        """(data_file_id, block_index, record) for every record pointing at this key."""
        out = []
        kinds = {1: 0x400, 2: 0x800}
        for pool_no, idx in self.find(key):
            for r in self.records:
                if r[0] == idx and r[2] != NO_POINTER and (r[7] & ~1) == kinds.get(pool_no, -1):
                    out.append((r[2] & 0xFFFF, (r[2] >> 16) & 0xFFFF, r))
        return out


def parse_manifest(path: Path) -> Manifest:
    data = zstd(path.read_bytes())
    v1, v2, unk0, n_pool2, n_table, n_pool3, n_pool1 = struct.unpack_from("<HHIIIII", data, 0)
    pos = 24
    pools = []
    for n in (n_pool1, n_pool2, n_pool3):
        pool = []
        for _ in range(n):
            ln = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            pool.append(data[pos : pos + ln].decode("utf-8", errors="replace"))
            pos = (pos + ln + 7) & ~7
        pools.append(pool)
    records = [struct.unpack_from("<8I", data, pos + i * 32) for i in range(n_table)]
    return Manifest(pools, records)


# ---------------------------------------------------------------- payload


class PayloadError(ValueError):
    pass


def tlv(buf: bytes, pos: int):
    tag = buf[pos]
    if tag in (0x0A, 0x09):
        n = struct.unpack_from("<H", buf, pos + 1)[0]
        pos += 3
        items = []
        for _ in range(n):
            v, pos = tlv(buf, pos)
            items.append(v)
        if tag == 0x0A and n % 2 == 0 and all(isinstance(items[i], str) for i in range(0, n, 2)):
            return {items[i]: items[i + 1] for i in range(0, n, 2)}, pos
        return items, pos
    if tag in (0x08, 0x28):
        n = struct.unpack_from("<H", buf, pos + 1)[0]
        raw = buf[pos + 3 : pos + 3 + n]
        return (raw.decode("utf-8", errors="replace") if tag == 0x08 else raw.hex()), pos + 3 + n
    simple = {0x01: ("<B", 1), 0x02: ("<H", 2), 0x03: ("<I", 4), 0x04: ("<Q", 8), 0x05: ("<f", 4), 0x06: ("<d", 8)}
    if tag in simple:
        fmt, size = simple[tag]
        return struct.unpack_from(fmt, buf, pos + 1)[0], pos + 1 + size
    lit = {0x0B: None, 0x0C: False, 0x0D: True}
    if tag in lit:
        return lit[tag], pos + 1
    raise PayloadError(f"unknown TLV tag {tag:#x} at {pos}")


def ftv(buf: bytes, pos: int):
    fid, t = buf[pos], buf[pos + 1]
    body = pos + 2
    if t in (0x00, 0x01):
        return (fid, t == 1), body
    simple = {0x04: ("<B", 1), 0x02: ("<H", 2), 0x0C: ("<I", 4), 0x10: ("<Q", 8), 0x14: ("<f", 4), 0x18: ("<d", 8)}
    if t in simple:
        fmt, size = simple[t]
        return (fid, struct.unpack_from(fmt, buf, body)[0]), body + size
    if t == 0x20:
        n = struct.unpack_from("<H", buf, body)[0]
        return (fid, buf[body + 2 : body + 2 + n].decode("utf-8", errors="replace")), body + 2 + n
    if t == 0x24:
        n = struct.unpack_from("<H", buf, body)[0]
        pos = body + 2
        items = []
        for _ in range(n):
            v, pos = tlv(buf, pos)
            items.append(v)
        return (fid, items), pos
    raise PayloadError(f"unknown FTV type {t:#x} at {pos}")


FTV_SIMPLE = {0x04: ("<B", 1), 0x02: ("<h", 2), 0x08: ("<H", 2), 0x0C: ("<I", 4), 0x10: ("<Q", 8),
              0x14: ("<f", 4), 0x18: ("<d", 8), 0x06: ("<b", 1), 0x0A: ("<i", 4), 0x0E: ("<q", 8)}


def ftv_token(buf: bytes, pos: int):
    """One table cell: u8 field index, u8 type, value. Types seen: bool 0/1, ints by width, f32/f64, str, TLV array."""
    fid, t = buf[pos], buf[pos + 1]
    body = pos + 2
    if t in (0x00, 0x01):
        return fid, t == 1, body
    if t in FTV_SIMPLE:
        fmt, size = FTV_SIMPLE[t]
        return fid, struct.unpack_from(fmt, buf, body)[0], body + size
    if t == 0x20:
        n = struct.unpack_from("<H", buf, body)[0]
        return fid, buf[body + 2 : body + 2 + n].decode("utf-8", errors="replace"), body + 2 + n
    if t == 0x24:
        n = struct.unpack_from("<H", buf, body)[0]
        pos = body + 2
        items = []
        for _ in range(n):
            v, pos = tlv(buf, pos)
            items.append(v)
        return fid, items, pos
    raise PayloadError(f"unknown cell type {t:#x} at {pos}")


def table_rows(buf: bytes) -> tuple[list[dict], int, str | None]:
    """
    Data-table block: rows of `u16 field_count` + cells, each row padded to 8 bytes.
    Returns (rows, bytes_consumed, error). Blocks may carry a trailing TLV section.
    """
    pos, rows, err = 0, [], None
    while pos + 2 <= len(buf):
        n = struct.unpack_from("<H", buf, pos)[0]
        if n == 0 or n > 120:
            break
        start = pos
        pos += 2
        row = {}
        try:
            for _ in range(n):
                fid, v, pos = ftv_token(buf, pos)
                row[fid] = v
        except (PayloadError, struct.error, IndexError) as e:
            err = f"{e} in row {len(rows)}"
            pos = start
            break
        rows.append(row)
        pos = (pos + 7) & ~7
    return rows, pos, err


def has_graph_header(buf: bytes, pos: int) -> bool:
    return len(buf) >= pos + 8 and struct.unpack_from("<HHHH", buf, pos)[0::3] == (0x0003, 0x0101) and buf[pos + 2 : pos + 4] == b"\x00\x28"


def decode_payload(buf: bytes) -> dict:
    pos = 8 if has_graph_header(buf, 0) else 0
    dialect = "tlv" if pos < len(buf) and buf[pos] in (0x0A, 0x09) else "ftv"
    items, error = [], None
    while pos < len(buf):
        if has_graph_header(buf, pos):
            pos += 8
            continue
        if dialect == "tlv" and buf[pos] == 0:
            pos += 1
            continue
        try:
            v, pos = (tlv if dialect == "tlv" else ftv)(buf, pos)
            items.append(v)
        except (PayloadError, struct.error, IndexError) as e:
            if dialect == "ftv" and buf[pos] == 0:
                pos += 1
                continue
            error = f"{e} (consumed {pos}/{len(buf)})"
            break
    return {"dialect": dialect, "size": len(buf), "error": error, "items": items}


# ---------------------------------------------------------------- cli


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hd_dir")
    ap.add_argument("--list", nargs="?", const=".", metavar="REGEX")
    ap.add_argument("--key")
    ap.add_argument("--block", metavar="dataN:IDX")
    ap.add_argument("--raw", action="store_true", help="write the raw decompressed block instead of JSON")
    ap.add_argument("--dump-tables", metavar="DIR", help="decode every data block as table rows into DIR/<file>_<block>.json + blocks.csv")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    hd = Path(args.hd_dir)

    if args.dump_tables:
        import csv
        out = Path(args.dump_tables)
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "blocks.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file", "block", "size", "rows", "nfields_max", "consumed", "error", "sample"])
            for df in sorted(hd.glob("data*"), key=lambda p: int(p.name[4:])):
                blocks = container_blocks(df)
                raw = df.read_bytes()
                for i, (s, e) in enumerate(blocks):
                    try:
                        payload = zstd(raw[s + 9 : e])
                    except Exception:
                        continue
                    rows, used, err = table_rows(payload)
                    if not rows:
                        continue
                    nf = max(len(r) for r in rows)
                    (out / f"{df.name}_{i}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                    w.writerow([df.name, i, len(payload), len(rows), nf, used, err or "", json.dumps(rows[0], ensure_ascii=False)[:200]])
                print(df.name, len(blocks), "blocks", flush=True)
        return

    man = parse_manifest(hd / "MANIFEST")

    if args.list:
        rx = re.compile(args.list)
        for p, pool in enumerate(man.pools, 1):
            for i, k in enumerate(pool):
                if rx.search(k):
                    ptrs = [(f"data{d}", b) for d, b, _ in man.pointers(k)]
                    print(f"pool{p}[{i}] {k} -> {ptrs}")
        return

    targets = []
    if args.block:
        name, idx = args.block.split(":")
        targets.append((name, int(idx)))
    if args.key:
        ptrs = man.pointers(args.key)
        if not ptrs:
            sys.exit(f"key not found or has no pointer records: {args.key}")
        targets += [(f"data{d}", b) for d, b, _ in ptrs]
    if not targets:
        ap.error("need --list, --key or --block")

    results = []
    for name, idx in targets:
        payload = read_block(hd / name, idx)
        if args.raw:
            Path(args.output or f"{name}_{idx}.bin").write_bytes(payload)
            print(f"{name}:{idx} -> {len(payload)} bytes raw")
            continue
        results.append({"block": f"{name}:{idx}", **decode_payload(payload)})
    if results:
        text = json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=1)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text[:20000])


if __name__ == "__main__":
    main()
