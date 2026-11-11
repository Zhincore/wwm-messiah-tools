#!/usr/bin/env python3
"""
Decode the resource repository: the engine's catalogue that maps resource names to GUIDs.

GUID-keyed files (textures, meshes, materials, ...) live in the VFS under `xx/<guid>[.ext]`, where
`xx` is the first two characters of the lowercase, dashed GUID. Textures use `.6` (a few `.1`),
other types have no extension. The repository names them.

Two VFS files, found here by content, share the same path stem (same lo32 tail signature):

  names file   u32 1, u32 0, u32 record_count, u8 section_count, then sections `u8 id, u32 length, payload`:
                 0  folders:    u16 0xFFFF, u32 length, ';'-separated folder paths
                 1  types:      u16 length, ';'-separated type names (Texture2D, LodModel, Material, ...)
                 2  GUID index: root + page table (key function unknown)
                 3  name index: root + page table; key = file_id("<folder>/<name>")
                 4  blocks:     u16 count, count x (u32 stored_size, u32 offset) into the data file
                 5, 6  bitmaps
               index section: u16 n, n x u64 first key per leaf page, u16 pages, pages x (u32 size, u32 offset);
               page 0 is blank
  data file    leaf pages: u16 count, count x u64 sorted keys, count x u32 values,
               value = (offset_in_block << 16) | block_number
               record blocks: u32 raw_size + one LZ4 block, 64 records each
  record       u16 kind, u16 variant, u8 0, GUID[16], u16 name_length, name,
               u16 folder_index, u16 type_index, u16 dependency_count, dependency GUIDs[16 each]

Usage:
  wwm_repository.py <install_root> --out repository.csv      every record: file id, GUID, type, path, deps
  wwm_repository.py <install_root> --find REGEX               records whose path matches
  wwm_repository.py <install_root> --deps PATH_OR_GUID        one record with its dependencies resolved
"""

from __future__ import annotations

import argparse
import csv
import re
import struct
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import lz4.block
import mmh3

import wwm_harvest_paths as hp

wt = hp.load_tool("wwm_textures")

MIN_DATA_SIZE = 1 << 20
HEX_DIGITS = frozenset(b"0123456789abcdef")
GUID_EXTENSIONS = ("", ".6", ".1")
SECTION_FOLDERS, SECTION_TYPES, SECTION_GUID_INDEX, SECTION_NAME_INDEX, SECTION_BLOCKS = range(5)


@dataclass(frozen=True, slots=True)
class Record:
    guid: bytes
    name: str
    folder: str
    type: str
    kind: int
    variant: int
    deps: tuple[bytes, ...]

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.name}"

    @property
    def guid_str(self) -> str:
        return str(uuid.UUID(bytes=self.guid))


def guid_file_id(guid: bytes | str, ext: str = "") -> int:
    """File id of the VFS path `xx/<guid><ext>`."""
    g = guid if isinstance(guid, str) else str(uuid.UUID(bytes=guid))
    path = f"{g[:2]}/{g}{ext}".encode()
    return (mmh3.hash(path, 0, signed=False) << 32) | struct.unpack(">I", path[3:7])[0]


def read_entry(entry, size: int | None = None) -> bytes:
    with open(entry.container, "rb") as f:
        f.seek(entry.offset)
        raw = f.read(entry.size if size is None else min(size, entry.size))
    return wt.ezst_decode(raw) if size is None and raw[:4] in (b"EZST", b"ZSTD") else raw


def is_names_file(head: bytes) -> bool:
    return len(head) >= 20 and head[:8] == b"\x01\0\0\0\0\0\0\0" and head[13] == SECTION_FOLDERS and head[18:20] == b"\xff\xff"


def locate(entries: list) -> tuple:
    """(names entry, data entry): a names-file signature plus a larger sibling with the same lo32."""
    by_lo = defaultdict(list)
    for e in entries:
        lo = e.file_id & 0xFFFFFFFF
        if not HEX_DIGITS.issuperset(lo.to_bytes(4, "big")):  # GUID-keyed files are never the repository
            by_lo[lo].append(e)
    for group in by_lo.values():
        big = [e for e in group if e.size >= MIN_DATA_SIZE]
        if len(group) < 2 or not big:
            continue
        for names in group:
            if not is_names_file(read_entry(names, 20)):
                continue
            data = max((e for e in big if e.file_id != names.file_id), key=lambda e: e.size, default=None)
            if data is not None:
                return names, data
    raise SystemExit("repository not found in this install")


def parse_sections(names: bytes) -> tuple[int, dict[int, bytes]]:
    _, _, count, n_sections = struct.unpack_from("<IIIB", names, 0)
    pos, sections = 13, {}
    for _ in range(n_sections):
        sid, length = struct.unpack_from("<BI", names, pos)
        sections[sid] = names[pos + 5 : pos + 5 + length]
        pos += 5 + length
    return count, sections


def string_list(section: bytes) -> list[str]:
    length = struct.unpack_from("<H", section, 0)[0]
    body = section[6 : 6 + struct.unpack_from("<I", section, 2)[0]] if length == 0xFFFF else section[2 : 2 + length]
    return body.decode("utf-8", "replace").split(";")


class Repository:
    def __init__(self, names: bytes, data: bytes):
        self.count, sections = parse_sections(names)
        self.folders = string_list(sections[SECTION_FOLDERS])
        self.types = string_list(sections[SECTION_TYPES])
        self.data = data
        blocks = sections[SECTION_BLOCKS]
        n = struct.unpack_from("<H", blocks, 0)[0]
        self.blocks = [struct.unpack_from("<II", blocks, 2 + 8 * i) for i in range(n)]

    @classmethod
    def from_entries(cls, entries: list) -> "Repository":
        names, data = locate(entries)
        return cls(read_entry(names), read_entry(data))

    def block(self, number: int) -> bytes:
        stored, offset = self.blocks[number]
        raw_size = struct.unpack_from("<I", self.data, offset)[0]
        return lz4.block.decompress(self.data[offset + 4 : offset + stored], uncompressed_size=raw_size)

    def records(self):
        for number in range(len(self.blocks)):
            buf, pos = self.block(number), 0
            while pos < len(buf):
                kind, variant = struct.unpack_from("<HH", buf, pos)
                guid = buf[pos + 5 : pos + 21]
                name_len = struct.unpack_from("<H", buf, pos + 21)[0]
                pos += 23
                name = buf[pos : pos + name_len].decode("utf-8", "replace")
                folder, type_index, n_deps = struct.unpack_from("<HHH", buf, pos + name_len)
                pos += name_len + 6
                deps = tuple(buf[pos + 16 * i : pos + 16 * i + 16] for i in range(n_deps))
                pos += 16 * n_deps
                yield Record(guid, name, self.folders[folder], self.types[type_index], kind, variant, deps)


def resolve(record: Record, ids: set[int]) -> tuple[int | None, str]:
    """(file id, extension) of the record's file when the install has it."""
    for ext in GUID_EXTENSIONS:
        fid = guid_file_id(record.guid, ext)
        if fid in ids:
            return fid, ext
    return None, ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root", type=Path)
    ap.add_argument("--out", type=Path, help="write every record to this CSV")
    ap.add_argument("--find", help="print records whose folder/name matches this regex")
    ap.add_argument("--deps", help="print one record (folder/name or GUID) with its dependencies")
    args = ap.parse_args()

    entries = wt.collect_entries(args.install_root, {"res", "lt", "cache", "db"})
    repo = Repository.from_entries(entries)
    ids = {e.file_id for e in entries}
    del entries
    print(f"{repo.count} records, {len(repo.folders)} folders, {len(repo.types)} types", file=sys.stderr)

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file_id", "ext", "guid", "type", "path", "kind", "variant", "deps"])
            for r in repo.records():
                fid, ext = resolve(r, ids)
                deps = " ".join(str(uuid.UUID(bytes=d)) for d in r.deps)
                w.writerow([f"{fid:016x}" if fid is not None else "", ext, r.guid_str, r.type, r.path, r.kind, r.variant, deps])
    if args.find:
        pattern = re.compile(args.find)
        for r in repo.records():
            if pattern.search(r.path):
                fid, ext = resolve(r, ids)
                print(f"{fid:016x}" if fid is not None else "-" * 16, r.guid_str + ext, r.type, r.path)
    if args.deps:
        by_guid = {r.guid: r for r in repo.records()}
        wanted = [r for r in by_guid.values() if args.deps in (r.path, r.guid_str)]
        for r in wanted:
            print(r.guid_str, r.type, r.path)
            for d in r.deps:
                dep = by_guid.get(d)
                print("   ", uuid.UUID(bytes=d), f"{dep.type} {dep.path}" if dep else "(not in repository)")
        if not wanted:
            sys.exit(f"no record for {args.deps}")


if __name__ == "__main__":
    main()
