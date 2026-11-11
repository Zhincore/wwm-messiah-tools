#!/usr/bin/env python3
"""
Read the HD store (Package/HD/<region>/: MANIFEST, data{n}, index{n}) that holds the game's
binary data tables ("MTable" tables such as chat_emotion_frame_data).

Layout:
  container  u32 magic 0xDEADBEEF, u32 version, u32 count, u32 reserved, u32 end_offsets[count]
             (relative to the data region); block = 9-byte header + zstd frame
  MANIFEST   same magic + one zstd frame. Payload: u16 v1, u16 v2, u32 unk, u32 n_tables, u32 n_shards,
             u32 n_extern, u32 n_other; three string pools (u16 len + bytes, 8-aligned): table names,
             extern names, other names; n_tables records of 8 x u32:
               rows, shard_count, schema_ptr, patch_schema_ptr, hash, 0, first_shard, kind
             (kind bit 0 = no schema, rows are bare TLV values); n_shards x (u32 ptr, u32 patch_ptr);
             then the index and data file name lists (u32 count, u16 len + bytes).
             ptr = (block << 16) | file number of an index file, 0xFFFFFFFF = none.
  schema     index block: u16 count, count x (u16 len + field name); a cell's field id is its position
  shard      index block holding a swiss-table hash map: u64 capacity, u64 n, u64 seed,
             capacity control bytes (< 0x80 = used) + 16 mirror bytes, 8-aligned, then capacity slots:
               u64 key, u16 data_file, u16 block, u16 offset/8, u16 length/8   (length 0 = row deleted)
             Patch shards override base shards key by key.
  row        u16 cell_count + cells: u8 field id, u8 type, value. type = (TLV tag << 2) | f,
             f = 1 adds 256 to the field id; the value is the TLV body without its tag byte
  TLV value  u8 tag: 0x01-0x04 u8/u16/u32/u64, 0x05 f32, 0x06 f64, 0x08 str (u16 len),
             0x09 list / 0x0a dict (u16 item count, items are TLVs; a dict alternates key, value),
             0x0b null, 0x0c false, 0x0d true
  locale     locale/translate_words_map_<lang>[_diff]: same container; block 0 = directory, text id lives in
             block 1 + id % (blocks - 1), a hash map with slots u64 id, u32 offset, u32 length; the offset
             is relative to the offset field itself. u64 cells holding text ids resolve through it.

Usage:
  wwm_hd.py <hd_dir> --list [REGEX]                 list tables: name, rows, shards, kind
  wwm_hd.py <hd_dir> --table NAME [-o out.json]     decode one table
  wwm_hd.py <hd_dir> --dump DIR [--match REGEX]     decode every (matching) table into DIR
  add --locale en (or zh_cn, ja, ...) to replace text ids with their strings
  wwm_hd.py <hd_dir> --block data5:123 -o out.bin   write one raw decompressed block
"""

from __future__ import annotations

import argparse
import json
import mmap
import re
import struct
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import zstandard

HD_MAGIC = 0xDEADBEEF
NONE = 0xFFFFFFFF
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
BLOCK_CACHE = 64
LOCALE_CACHE = 1024


def zstd(data: bytes) -> bytes:
    i = data.find(ZSTD_MAGIC)
    if i < 0:
        raise ValueError("no zstd frame")
    return zstandard.ZstdDecompressor().decompressobj().decompress(data[i:])


# ---------------------------------------------------------------- manifest


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    rows: int
    shard_count: int
    schema: int
    patch_schema: int
    first_shard: int
    kind: int


@dataclass
class Manifest:
    tables: dict[str, Table]
    shards: list[tuple[int, int]]
    extern: list[str]
    other: list[str]


def _strings(data: bytes, pos: int, n: int, align: bool) -> tuple[list[str], int]:
    out = []
    for _ in range(n):
        ln = struct.unpack_from("<H", data, pos)[0]
        out.append(data[pos + 2 : pos + 2 + ln].decode("utf-8", errors="replace"))
        pos += 2 + ln
        if align:
            pos = (pos + 7) & ~7
    return out, pos


def parse_manifest(path: Path) -> Manifest:
    data = zstd(path.read_bytes())
    _, _, _, n_tables, n_shards, n_extern, n_other = struct.unpack_from("<HHIIIII", data, 0)
    names, pos = _strings(data, 24, n_tables, True)
    extern, pos = _strings(data, pos, n_extern, True)
    other, pos = _strings(data, pos, n_other, True)
    tables = {}
    for i, name in enumerate(names):
        rows, shard_count, schema, patch_schema, _, _, first, kind = struct.unpack_from("<8I", data, pos + 32 * i)
        tables[name] = Table(name, rows, shard_count, schema, patch_schema, first, kind)
    pos += 32 * n_tables
    shards = [struct.unpack_from("<II", data, pos + 8 * i) for i in range(n_shards)]
    return Manifest(tables, shards, extern, other)


# ---------------------------------------------------------------- store


class Container:
    """One index/data file, mmapped; blocks are decompressed on demand."""

    def __init__(self, path: Path):
        self._f = open(path, "rb")
        self._mm = mmap.mmap(self._f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, _, count, _ = struct.unpack_from("<IIII", self._mm, 0)
        if magic != HD_MAGIC:
            raise ValueError(f"{path}: bad magic")
        self._ends = struct.unpack_from(f"<{count}I", self._mm, 16)
        self._base = 16 + 4 * count

    def __len__(self) -> int:
        return len(self._ends)

    def block(self, index: int) -> bytes:
        start = self._base + (self._ends[index - 1] if index else 0)
        return zstd(self._mm[start + 9 : self._base + self._ends[index]])


class Store:
    def __init__(self, hd_dir: Path, locale: Locale | None = None):
        self.dir = hd_dir
        self.locale = locale
        self.manifest = parse_manifest(hd_dir / "MANIFEST")
        self._files: dict[str, Container] = {}
        self._blocks: OrderedDict[tuple[str, int], bytes] = OrderedDict()

    def block(self, kind: str, file_no: int, index: int) -> bytes:
        name = f"{kind}{file_no}"
        key = (name, index)
        if key in self._blocks:
            self._blocks.move_to_end(key)
            return self._blocks[key]
        if name not in self._files:
            self._files[name] = Container(self.dir / name)
        buf = self._files[name].block(index)
        self._blocks[key] = buf
        if len(self._blocks) > BLOCK_CACHE:
            self._blocks.popitem(last=False)
        return buf

    def _ptr(self, ptr: int) -> bytes:
        return self.block("index", ptr & 0xFFFF, ptr >> 16)

    def schema(self, ptr: int) -> list[str]:
        if ptr == NONE:
            return []
        buf = self._ptr(ptr)
        return _strings(buf, 2, struct.unpack_from("<H", buf, 0)[0], False)[0]

    def shard(self, ptr: int) -> list[tuple[int, int, int, int, int]]:
        """Used slots of a hash-map block: (key, data_file, block, offset8, length8)."""
        buf = self._ptr(ptr)
        cap = struct.unpack_from("<Q", buf, 0)[0]
        base = (24 + cap + 16 + 7) & ~7
        return [struct.unpack_from("<QHHHH", buf, base + 16 * i) for i in range(cap) if buf[24 + i] < 0x80]

    def locations(self, t: Table) -> list[tuple[int, int, int, int, int, bool]]:
        """(key, data_file, block, offset8, length8, from_patch) per live row. Keys repeat across shards."""
        out = []
        for base, patch in self.manifest.shards[t.first_shard : t.first_shard + t.shard_count]:
            rows: dict[int, tuple[int, int, int, int, bool]] = {}
            for ptr, patched in ((base, False), (patch, True)):
                if ptr == NONE:
                    continue
                for key, f, b, off, ln in self.shard(ptr):
                    if ln:
                        rows[key] = (f, b, off, ln, patched)
                    else:
                        rows.pop(key, None)
            out.extend((key, *v) for key, v in rows.items())
        return out

    def table(self, name: str) -> dict:
        t = self.manifest.tables[name]
        fields = self.schema(t.schema)
        patch_fields = self.schema(t.patch_schema) or fields
        rows, errors = [], 0
        for key, f, b, off, ln, patched in sorted(self.locations(t), key=lambda r: r[1:4]):
            seg = self.block("data", f, b)[off * 8 : (off + ln) * 8]
            try:
                value = decode_value(seg, patch_fields if patched else fields, bool(t.kind & 1))
            except (PayloadError, struct.error, IndexError) as e:
                value, errors = {"_error": str(e), "_raw": seg.hex()}, errors + 1
            if self.locale:
                value = self.locale.resolve(value)
            rows.append({"_key": key, "value": value} if not isinstance(value, dict) else {"_key": key, **value})
        rows.sort(key=lambda r: r["_key"])
        return {"name": name, "kind": t.kind, "fields": fields, "errors": errors, "rows": rows}


class Locale:
    """Text id -> string for one language; the _diff file overrides the base file."""

    def __init__(self, hd_dir: Path, lang: str):
        paths = [hd_dir / "locale" / f"translate_words_map_{lang}{sfx}" for sfx in ("_diff", "")]
        self._files = [Container(p) for p in paths if p.exists()]
        if not self._files:
            raise FileNotFoundError(f"no locale files for {lang!r} in {hd_dir / 'locale'}")
        self._shards: OrderedDict[tuple[int, int], dict[int, str]] = OrderedDict()

    def _shard(self, file_no: int, index: int) -> dict[int, str]:
        key = (file_no, index)
        if key in self._shards:
            self._shards.move_to_end(key)
            return self._shards[key]
        buf = self._files[file_no].block(index)
        cap = struct.unpack_from("<Q", buf, 0)[0]
        base = (24 + cap + 16 + 7) & ~7
        out = {}
        for i in range(cap):
            if buf[24 + i] < 0x80:
                pos = base + 16 * i
                tid, off, ln = struct.unpack_from("<QII", buf, pos)
                out[tid] = buf[pos + 8 + off : pos + 8 + off + ln].decode("utf-8", errors="replace")
        self._shards[key] = out
        if len(self._shards) > LOCALE_CACHE:
            self._shards.popitem(last=False)
        return out

    def text(self, tid: int) -> str | None:
        for n, c in enumerate(self._files):
            s = self._shard(n, 1 + tid % (len(c) - 1)).get(tid)
            if s is not None:
                return s
        return None

    def resolve(self, value):
        """Replace every 64-bit integer that is a known text id, recursively."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return (self.text(value) or value) if value > 0xFFFFFFFF else value
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, dict):
            return {k: (v if k == "_key" else self.resolve(v)) for k, v in value.items()}
        return value


# ---------------------------------------------------------------- payload


class PayloadError(ValueError):
    pass


TLV_NUM = {0x01: ("<B", 1), 0x02: ("<H", 2), 0x03: ("<I", 4), 0x04: ("<Q", 8), 0x05: ("<f", 4), 0x06: ("<d", 8)}
TLV_LIT = {0x0B: None, 0x0C: False, 0x0D: True}
TLV_STR, TLV_LIST, TLV_DICT = 0x08, 0x09, 0x0A


def tlv_body(tag: int, buf: bytes, pos: int):
    """Value of a TLV whose tag byte was already consumed; returns (value, next position)."""
    if tag in TLV_NUM:
        fmt, size = TLV_NUM[tag]
        return struct.unpack_from(fmt, buf, pos)[0], pos + size
    if tag in TLV_LIT:
        return TLV_LIT[tag], pos
    if tag == TLV_STR:
        n = struct.unpack_from("<H", buf, pos)[0]
        return buf[pos + 2 : pos + 2 + n].decode("utf-8", errors="replace"), pos + 2 + n
    if tag in (TLV_LIST, TLV_DICT):
        n = struct.unpack_from("<H", buf, pos)[0]
        pos += 2
        items = []
        for _ in range(n):
            v, pos = tlv(buf, pos)
            items.append(v)
        keys = items[0::2]
        if tag == TLV_DICT and n % 2 == 0 and all(isinstance(k, (str, int)) and not isinstance(k, bool) for k in keys):
            return {str(k): v for k, v in zip(keys, items[1::2])}, pos
        return items, pos
    raise PayloadError(f"unknown TLV tag {tag:#x} at {pos - 1}")


def tlv(buf: bytes, pos: int):
    return tlv_body(buf[pos], buf, pos + 1)


def cell(buf: bytes, pos: int):
    """One row cell: u8 field id, u8 type, value. type = (TLV tag << 2) | bit 0 = field id + 256."""
    fid, t = buf[pos], buf[pos + 1]
    if t & 2:
        raise PayloadError(f"unknown cell type {t:#x} at {pos}")
    value, pos = tlv_body(t >> 2, buf, pos + 2)
    return fid | ((t & 1) << 8), value, pos


def decode_value(seg: bytes, fields: list[str], bare: bool):
    """A stored row: bare TLV value (schemaless tables) or `u16 cell_count` + cells keyed by field name."""
    if bare:
        return tlv(seg, 0)[0]
    n = struct.unpack_from("<H", seg, 0)[0]
    pos, row = 2, {}
    for _ in range(n):
        fid, v, pos = cell(seg, pos)
        row[fields[fid] if fid < len(fields) else f"#{fid}"] = v
    return row


# ---------------------------------------------------------------- cli


def out_path(root: Path, name: str) -> Path:
    """Tables are grouped by their first dotted component (spaces.s1.x -> spaces/s1.x.json)."""
    head, _, rest = name.partition(".")
    safe = re.sub(r"[^\w.\-]", "_", rest or head)
    return root / head / f"{safe}.json" if rest else root / f"{safe}.json"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hd_dir")
    ap.add_argument("--list", nargs="?", const=".", metavar="REGEX")
    ap.add_argument("--table")
    ap.add_argument("--dump", metavar="DIR")
    ap.add_argument("--match", default=".", metavar="REGEX", help="table name filter for --dump")
    ap.add_argument("--block", metavar="FILE:IDX", help="e.g. data5:123 or index1:7")
    ap.add_argument("--locale", metavar="LANG", help="resolve text ids, e.g. en, zh_cn")
    ap.add_argument("-o", "--output")
    args = ap.parse_args()
    hd = Path(args.hd_dir)
    store = Store(hd, Locale(hd, args.locale) if args.locale else None)
    tables = store.manifest.tables

    if args.list:
        rx = re.compile(args.list)
        for t in tables.values():
            if rx.search(t.name):
                print(f"{t.name}\trows={t.rows}\tshards={t.shard_count}\tkind={t.kind:#x}")
        return

    if args.block:
        name, idx = args.block.split(":")
        kind = name.rstrip("0123456789")
        buf = store.block(kind, int(name[len(kind):]), int(idx))
        Path(args.output or f"{name}_{idx}.bin").write_bytes(buf)
        print(f"{args.block}: {len(buf)} bytes")
        return

    if args.table:
        if args.table not in tables:
            sys.exit(f"no such table: {args.table}")
        text = json.dumps(store.table(args.table), ensure_ascii=False, indent=1)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"wrote {args.output}")
        else:
            print(text)
        return

    if args.dump:
        import csv
        root = Path(args.dump)
        root.mkdir(parents=True, exist_ok=True)
        rx = re.compile(args.match)
        picked = [t for t in tables.values() if rx.search(t.name)]
        with open(root / "tables.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["name", "kind", "rows_declared", "rows", "errors", "fields"])
            for i, t in enumerate(picked, 1):
                try:
                    res = store.table(t.name)
                except Exception as e:
                    w.writerow([t.name, hex(t.kind), t.rows, -1, str(e), ""])
                    continue
                p = out_path(root, t.name)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
                w.writerow([t.name, hex(t.kind), t.rows, len(res["rows"]), res["errors"], " ".join(res["fields"])])
                if i % 5000 == 0:
                    print(f"{i}/{len(picked)}", flush=True)
        print(f"dumped {len(picked)} tables to {root}")
        return

    ap.error("need --list, --table, --dump or --block")


if __name__ == "__main__":
    main()
