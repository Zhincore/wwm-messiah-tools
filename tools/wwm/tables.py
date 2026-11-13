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
             Patch shards override base shards key by key. Keys repeat across shards and, in grouped
             tables, within one. The manifest row count can differ slightly for patched tables.
  row        u16 cell_count + cells: u8 field id, u8 type, value. type = (TLV tag << 2) | f,
             f = bits 8-9 of the field id; the value is the TLV body without its tag byte
  TLV value  u8 tag: 0x01-0x04 u8/u16/u32/u64, 0x05 f32, 0x06 f64, 0x08 str (u16 len),
             0x09 list / 0x0a dict (u16 item count, items are TLVs; a dict alternates key, value),
             0x0b null, 0x0c false, 0x0d true
  locale     locale/translate_words_map_<lang>[_diff]: same container; block 0 = directory, text id lives in
             block 1 + id % (blocks - 1), a hash map with slots u64 id, u32 offset, u32 length; the offset
             is relative to the offset field itself. u64 cells holding text ids resolve through it.

Usage:
  wwm-tables <hd_dir> --list [REGEX]                 list tables: name, rows, shards, kind
  wwm-tables <hd_dir> --table NAME [-o out.json]     decode one table
  wwm-tables <hd_dir> --dump DIR [--match REGEX]     decode every (matching) table into DIR
  add --locale en (or zh_cn, ja, ...) to replace text ids with their strings
  wwm-tables <hd_dir> --block data5:123 -o out.bin   write one raw decompressed block
  wwm-tables <hd_dir> --patch-field TABLE --where F=V --field exec_func --value LUA [--apply]
                                                     in-place edit a string cell of one row (same length,
                                                     re-zstd to the exact stored size, journalled); game closed
  wwm-tables <hd_dir> --revert-patch JOURNAL         undo a --patch-field change
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
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
        """
        (key, data_file, block, offset8, length8, from_patch) per live row. Keys are not unique:
        they repeat across shards, and grouped tables store several rows under one key.
        """
        out = []
        for base, patch in self.manifest.shards[t.first_shard : t.first_shard + t.shard_count]:
            patch_slots = self.shard(patch) if patch != NONE else []
            replaced = {slot[0] for slot in patch_slots}
            if base != NONE:
                out.extend((*slot, False) for slot in self.shard(base) if slot[0] not in replaced)
            out.extend((*slot, True) for slot in patch_slots if slot[4])
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
    """One row cell: u8 field id, u8 type, value. type = (TLV tag << 2) | bits 8-9 of the field id."""
    fid, t = buf[pos], buf[pos + 1]
    value, pos = tlv_body(t >> 2, buf, pos + 2)
    return fid | ((t & 3) << 8), value, pos


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


# ---------------------------------------------------------------- write-back


def string_cell_span(seg: bytes, fields: list[str], field: str) -> tuple[int, int]:
    """Byte range [start, end) of a string cell's VALUE (no length prefix) within a row segment.

    Only string cells are supported; raises if the field is absent or not a string.
    """
    target = fields.index(field)
    n = struct.unpack_from("<H", seg, 0)[0]
    pos = 2
    for _ in range(n):
        fid_lo, t = seg[pos], seg[pos + 1]
        fid = fid_lo | ((t & 3) << 8)
        tag = t >> 2
        vpos = pos + 2
        if tag == TLV_STR:
            ln = struct.unpack_from("<H", seg, vpos)[0]
            if fid == target:
                return vpos + 2, vpos + 2 + ln
            pos = vpos + 2 + ln
        else:
            _, pos = tlv_body(tag, seg, vpos)
    raise ValueError(f"field {field!r} not found as a string cell in the row")


def read_container(path: Path) -> tuple[tuple[int, int, int], list[bytes]]:
    """(magic, version, reserved), stored block bytes[] of a container file."""
    data = path.read_bytes()
    magic, version, count, reserved = struct.unpack_from("<IIII", data, 0)
    if magic != HD_MAGIC:
        raise ValueError(f"{path}: bad magic")
    ends = struct.unpack_from(f"<{count}I", data, 16)
    base = 16 + 4 * count
    blocks = [data[base + (ends[i - 1] if i else 0) : base + ends[i]] for i in range(count)]
    return (magic, version, reserved), blocks


def write_container(path: Path, header: tuple[int, int, int], blocks: list[bytes]) -> None:
    """Write a container, recomputing end_offsets. Block count/order unchanged keeps shard refs valid."""
    magic, version, reserved = header
    ends, acc = [], 0
    for b in blocks:
        acc += len(b)
        ends.append(acc)
    out = struct.pack("<IIII", magic, version, len(blocks), reserved) + struct.pack(f"<{len(blocks)}I", *ends)
    out += b"".join(blocks)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(out)
    os.replace(tmp, path)


def block_header9(container: Container, index: int) -> bytes:
    start = container._base + (container._ends[index - 1] if index else 0)
    return bytes(container._mm[start : start + 9])


def locate_row(store: Store, table: str, where_field: str, where_value: str):
    """The unique row matching where_field==where_value; returns its physical location."""
    t = store.manifest.tables[table]
    fields = store.schema(t.schema)
    hits = []
    for key, f, b, off, ln, patched in store.locations(t):
        seg = store.block("data", f, b)[off * 8 : (off + ln) * 8]
        row = decode_value(seg, fields, bool(t.kind & 1))
        if isinstance(row, dict) and str(row.get(where_field)) == where_value:
            hits.append((key, f, b, off, ln, patched, row))
    if not hits:
        sys.exit(f"no row in {table} with {where_field}={where_value!r}")
    if len(hits) > 1:
        sys.exit(f"{len(hits)} rows match {where_field}={where_value!r}; need a unique selector")
    return t, fields, hits[0]


def patch_field(args) -> None:
    """Replace a string field of one row (same decoded length), rewriting the data block; journalled.

    The decompressed block keeps its size (so shard offsets are unchanged), but its recompressed
    stored size may differ, so the whole container is rewritten with updated end_offsets.
    """
    from .codecs import zstd_frame
    from .files import game_running

    if args.apply and not args.force and game_running():
        sys.exit("wwm.exe is running; close the game first (it reads the HD store at startup) or pass --force")
    hd = Path(args.hd_dir)
    store = Store(hd)
    t, fields, (key, f, b, off, ln, patched, row) = locate_row(store, args.patch_field, args.where_field, args.where_value)
    data_name = f"data{f}"
    container = store._files.get(data_name) or Container(hd / data_name)
    block = bytearray(container.block(b))
    seg_start = off * 8
    vs, ve = string_cell_span(block[seg_start : (off + ln) * 8], fields, args.field)
    vs, ve = seg_start + vs, seg_start + ve
    old = bytes(block[vs:ve]).decode("utf-8", "replace")
    new_bytes = args.value.encode("utf-8")
    if len(new_bytes) > ve - vs:
        sys.exit(f"replacement is {len(new_bytes)} bytes; field holds {ve - vs} (same decoded length only; shorten it)")
    new_bytes = new_bytes + b" " * (ve - vs - len(new_bytes))  # pad with spaces (harmless in Lua)
    block[vs:ve] = new_bytes
    header9 = block_header9(container, b)
    new_stored = header9 + zstd_frame(bytes(block))  # natural size; container rewrite absorbs the delta
    path = hd / data_name
    print(f"table    {args.patch_field}  row {args.where_field}={args.where_value!r} (key {key:#x})")
    print(f"field    {args.field}: {old.strip()!r}")
    print(f"      -> {args.value!r} (padded to {ve - vs} bytes)")
    print(f"block    {data_name} block {b}{' [patch shard]' if patched else ''}")
    if not args.apply:
        print("dry run: nothing written (add --apply)")
        return
    hdr, blocks = read_container(path)
    jdir = Path(args.journal_dir)
    jdir.mkdir(parents=True, exist_ok=True)
    jpath = jdir / f"hdpatch_{args.patch_field}_{re.sub(r'[^A-Za-z0-9]+', '_', args.where_value)}.json"
    if jpath.exists():
        sys.exit(f"a patch for this row is already journalled ({jpath}); revert it first")
    jpath.write_text(json.dumps({"file": data_name, "block": b, "orig": blocks[b].hex(),
                                 "table": args.patch_field, "where": f"{args.where_field}={args.where_value}",
                                 "field": args.field}))
    blocks[b] = new_stored
    write_container(path, hdr, blocks)
    print(f"applied. journal: {jpath}")


def revert_patch(args) -> None:
    j = json.loads(Path(args.revert_patch).read_text())
    path = Path(args.hd_dir) / j["file"]
    hdr, blocks = read_container(path)
    blocks[j["block"]] = bytes.fromhex(j["orig"])
    write_container(path, hdr, blocks)
    Path(args.revert_patch).rename(Path(args.revert_patch).with_suffix(".reverted.json"))
    print(f"reverted {j['table']} {j['where']} {j['field']} (block {j['block']} in {j['file']})")


# ---------------------------------------------------------------- cli

DEFAULT_JOURNAL_DIR = Path.home() / ".local/share/wwm-messiah-tools/hd-patches"


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
    ap.add_argument("--patch-field", metavar="TABLE", help="in-place edit a string field of one row (game closed)")
    ap.add_argument("--where", metavar="FIELD=VALUE", help="row selector for --patch-field")
    ap.add_argument("--field", help="field to replace (string cell), e.g. exec_func")
    ap.add_argument("--value", help="new field value; must be <= the current length (padded with spaces)")
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    ap.add_argument("--journal-dir", default=str(DEFAULT_JOURNAL_DIR))
    ap.add_argument("--force", action="store_true", help="skip the game-running check for --patch-field")
    ap.add_argument("--revert-patch", metavar="JOURNAL", help="undo a --patch-field change from its journal")
    args = ap.parse_args()
    hd = Path(args.hd_dir)

    if args.revert_patch:
        return revert_patch(args)
    if args.patch_field:
        if not args.where or "=" not in args.where or not args.field or args.value is None:
            ap.error("--patch-field needs --where FIELD=VALUE --field NAME --value STR")
        args.where_field, _, args.where_value = args.where.partition("=")
        return patch_field(args)

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
