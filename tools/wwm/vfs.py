"""
Index layer: every index source of an install, resolved to containers, and pack access.

  Resources.mpkinfo             -> Resources.mpk            (install root)
  LocalData/Patch/LT{n}.mpkinfo -> LT{n}.mpk                (patch root)
  LocalData/Patch/*.mpkinfo     -> Patch{pack_id}.mpk       (per entry; MpkCached_*, TinyFiles_*, Patch{n})
  LocalData/Patch/Patch.mpkdb   -> Patch{v2}.mpk            (SQLite, tables info0 + info1)
"""

from __future__ import annotations

import mmap
import os
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

from .codecs import ezst_decode

TEX_MAGIC = b"\x02\x02\x02\x01"


@dataclass(frozen=True, slots=True)
class Entry:
    file_id: int
    offset: int
    size: int
    container: str  # absolute path of the .mpk
    source: str  # index family label: res / lt / cache / db0 / db1
    index_name: str


def _container_for_index(index_path: Path, pack_id: int, install_root: Path) -> Path:
    stem = index_path.stem
    if stem == "Resources":
        name = "Resources.mpk" if pack_id == 0 else f"Resources{pack_id}.mpk"
        return install_root / name
    if re.fullmatch(r"LT\d+", stem):
        return index_path.parent / f"LT{pack_id}.mpk"
    return index_path.parent / f"Patch{pack_id}.mpk"


def read_mpkinfo(path: Path, install_root: Path, source: str) -> list[Entry]:
    data = path.read_bytes()
    if data[:4] in (b"HEX_", b"HEXB"):
        return []  # wrapped integrity metadata, not an index
    if len(data) < 8:
        return []
    version, count = struct.unpack_from("<II", data, 0)
    if version != 3 or len(data) != 8 + 20 * count + 16:
        return []
    out = []
    containers: dict[int, str] = {}
    for s0, s1, off, size, packed in struct.iter_unpack("<IIIII", data[8 : 8 + 20 * count]):
        if size == 0 or packed & 1:
            continue
        pack_id = packed >> 1
        container = containers.get(pack_id)
        if container is None:
            container = containers[pack_id] = str(_container_for_index(path, pack_id, install_root))
        out.append(Entry((s1 << 32) | s0, off, size, container, source, path.name))
    return out


def read_mpkdb(path: Path) -> list[Entry]:
    # mode=ro (not immutable): the schema and rows can live in the -wal, which immutable mode ignores.
    out = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, source in (("info0", "db0"), ("info1", "db1")):
            try:
                rows = con.execute(f"select v0, v1, v2, v6 from {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            for v0, size, pack, off in rows:
                if not size or isinstance(v0, str):
                    continue
                fid = v0 & 0xFFFFFFFFFFFFFFFF
                out.append(Entry(fid, off, size, str(path.parent / f"Patch{pack}.mpk"), source, table))
    finally:
        con.close()
    return out


def collect_entries(install_root: Path, sources: set[str]) -> list[Entry]:
    patch_root = install_root / "LocalData" / "Patch"
    entries: list[Entry] = []
    if "res" in sources:
        p = install_root / "Resources.mpkinfo"
        if p.exists():
            entries += read_mpkinfo(p, install_root, "res")
    if patch_root.is_dir():
        for p in sorted(patch_root.glob("*.mpkinfo")):
            if re.fullmatch(r"LT\d+", p.stem):
                if "lt" in sources:
                    entries += read_mpkinfo(p, install_root, "lt")
            elif "cache" in sources:
                entries += read_mpkinfo(p, install_root, "cache")
        db = patch_root / "Patch.mpkdb"
        if "db" in sources and db.exists():
            entries += read_mpkdb(db)
    exists = {c: os.path.exists(c) for c in {e.container for e in entries}}
    return [e for e in entries if exists[e.container]]


@dataclass(frozen=True, slots=True)
class Block:
    phys_off: int
    phys_size: int
    raw_size: int


class Pack:
    """
    One .mpk container, memory-mapped.

    Plain packs: index offsets are physical. Block packs: every index entry is one EZST
    block whose `size` is the physical block size. A partial ("lite") install keeps only
    some blocks, compacted together, so index offsets do not apply; the blocks are walked
    in order instead and matched to entries by size. A pack is treated as a block pack
    only when it starts with EZST and its index entries do not tile the file exactly.
    Block contents are decoded on demand (`block_data`), never kept resident.
    """

    def __init__(self, path: str, entries: list[Entry] | None = None):
        self.path = path
        self.fh = open(path, "rb")
        self.fd = self.fh.fileno()
        self.size = os.fstat(self.fd).st_size
        self.blocks: list[Block] | None = None
        if self.read(0, 4) == b"EZST" and self.size <= self.MAX_WALK and not self._index_is_physical(entries):
            self.blocks = self._walk_blocks()

    KNOWN_HEADS = (b"EZST", b"ZSTD", TEX_MAGIC, b"RIFF", b"BKHD", b"ZZZ4", b"LZMA", b".MES", b"<?xm", b"CCCC")

    def _index_is_physical(self, entries: list[Entry] | None) -> bool:
        """
        True when index offsets address this file directly: every entry lies inside the
        file, entries tile it (mostly zero gaps) or start with known magics, and sampled
        EZST entries really decode at their offsets (a compacted lite pack fails this).
        """
        if not entries:
            return False
        if max(e.offset + e.size for e in entries) > self.size:
            return False
        ordered = sorted(entries, key=lambda e: e.offset)
        zero_gaps = sum(1 for a, b in zip(ordered, ordered[1:]) if b.offset == a.offset + a.size)
        step = max(1, len(ordered) // 200)
        sample = ordered[::step]
        heads = [(e, self.read(e.offset, 4)) for e in sample]
        known = sum(1 for _, h in heads if h in self.KNOWN_HEADS)
        if zero_gaps < 0.5 * max(1, len(ordered) - 1) and known < 0.9 * len(sample):
            return False
        ezst = [e for e, h in heads if h == b"EZST"][:10]
        if ezst:
            ok = 0
            for e in ezst:
                try:
                    ezst_decode(self.read(e.offset, min(e.size, self.MAX_BLOCK)))
                    ok += 1
                except Exception:
                    pass
            return ok >= 0.8 * len(ezst)
        return True

    MAX_BLOCK = 64 << 20
    MAX_WALK = 256 << 20  # block packs are small; never walk a big physical pack

    def _walk_blocks(self) -> list[Block] | None:
        """
        Walk back-to-back EZST blocks. "EZST" can occur inside compressed data, so a
        candidate block end is accepted only if the block decodes; a start that never
        decodes is a false positive and the walk resyncs. Packs may end with a tail.
        """
        mm = mmap.mmap(self.fd, 0, access=mmap.ACCESS_READ)
        try:
            data = mm
            blocks: list[Block] = []
            pos = 0
            covered = 0
            while pos != -1 and pos < len(data):
                if data[pos : pos + 4] != b"EZST":
                    break
                nxt = data.find(b"EZST", pos + 4)
                decoded = None
                while True:
                    end = len(data) if nxt == -1 else nxt
                    if end - pos > self.MAX_BLOCK:
                        break
                    try:
                        decoded = ezst_decode(bytes(data[pos:end]))
                        break
                    except Exception:
                        if nxt == -1:
                            break
                        nxt = data.find(b"EZST", nxt + 1)
                if decoded is None:
                    pos = data.find(b"EZST", pos + 1)
                    continue
                blocks.append(Block(pos, end - pos, len(decoded)))
                covered += end - pos
                pos = end
        finally:
            mm.close()
        if not blocks or covered < self.size // 2:
            return None
        return blocks

    def read(self, off: int, size: int) -> bytes:
        return os.pread(self.fd, size, off)

    def block_data(self, block: Block) -> bytes:
        return ezst_decode(self.read(block.phys_off, block.phys_size))

    def match_blocks(self, entries: list[Entry]) -> list[tuple[Block, Entry | None]]:
        """Greedily align physical blocks with offset-ordered index entries by size."""
        assert self.blocks is not None
        ordered = sorted(entries, key=lambda e: e.offset)
        out: list[tuple[Block, Entry | None]] = []
        i = 0
        for block in self.blocks:
            j = i
            while j < len(ordered) and ordered[j].size != block.phys_size:
                j += 1
            if j < len(ordered):
                out.append((block, ordered[j]))
                i = j + 1
            else:
                out.append((block, None))
        return out
