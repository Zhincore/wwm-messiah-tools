"""Core behaviour on synthetic data: file ids, codecs, pack layouts, and the override round trip."""

from __future__ import annotations

import random
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from wwm import codecs, ids, vfs

SCHEMA = "CREATE TABLE {} (v0 INT PRIMARY KEY, v1 INT NOT NULL, v2 INT NOT NULL, v3 INT NOT NULL, " \
         "v4 BOOL NOT NULL, v5 BOOL NOT NULL, v6 INT NOT NULL)"
ANIM = "Char/test/b_idle.anim"
SCRIPT = "LuaText/hexm/common/strict.lua"
ANIM_DATA = b"anim keyframes " * 400
CHUNK = codecs.CHUNK_HEADER + b"\x00" + b"bytecode" * 20


@pytest.mark.parametrize("path, expected", [
    ("LuaText/patch/patch_main.lua", 0x5A6213136E696118),
    ("LuaText/checker.lua", 0x1B16D3FA72656B0F),
    ("LuaText/hexm/common/strict.lua", 0xB5FEDBC77463691A),
])
def test_file_id_matches_engine_index(path, expected):
    assert ids.file_id(path) == expected


@pytest.mark.parametrize("magic", [b"EZST", b"ZSTD"])
def test_ezst_exact_size(magic):
    data = bytes(range(256)) * 64
    natural = len(codecs.ezst_encode(data, magic))
    for gap in range(13):
        if gap in (1, 4):  # no header/padding combination reaches these
            with pytest.raises(ValueError):
                codecs.ezst_encode(data, magic, size=natural + gap)
            continue
        block = codecs.ezst_encode(data, magic, size=natural + gap)
        assert len(block) == natural + gap
        assert codecs.ezst_decode(block) == data
    with pytest.raises(ValueError):
        codecs.ezst_encode(data, magic, size=natural - 40)


def test_luatext_bytecode_and_source():
    assert codecs.luatext_decode(codecs.luatext_encode(CHUNK)) == CHUNK
    source = codecs.luatext_encode(b"return 1\n")
    assert codecs.luatext_decode(source) is None
    assert codecs.luatext_decode(source, require_chunk=False) == b"return 1\n"


def write_mpkinfo(path: Path, rows: list[tuple[int, int, int, int]]) -> None:
    """rows: (file_id, offset, size, pack_id)"""
    body = b"".join(struct.pack("<IIIII", fid & 0xFFFFFFFF, fid >> 32, off, size, pack << 1)
                    for fid, off, size, pack in rows)
    path.write_bytes(struct.pack("<II", 3, len(rows)) + body + bytes(16))


def test_compacted_block_pack_is_walked(tmp_path):
    payloads = [random.Random(i).randbytes(1000 + i * 300) for i in range(4)]  # incompressible: distinct sizes
    blocks = [codecs.ezst_encode(p) for p in payloads]
    kept = [blocks[0], blocks[2], blocks[3]]  # lite install: block 1 missing, rest compacted
    offsets = [sum(len(b) for b in blocks[:i]) for i in range(4)]
    patch = tmp_path / "LocalData" / "Patch"
    patch.mkdir(parents=True)
    (patch / "Patch7.mpk").write_bytes(b"".join(kept))
    write_mpkinfo(patch / "MpkCached_test.mpkinfo", [(100 + i, offsets[i], len(b), 7) for i, b in enumerate(blocks)])
    entries = vfs.collect_entries(tmp_path, {"cache"})
    pack = vfs.Pack(str(patch / "Patch7.mpk"), entries)
    assert pack.blocks is not None and len(pack.blocks) == 3
    matched = [(pack.block_data(b), e.file_id) for b, e in pack.match_blocks(entries)]
    assert [fid for _, fid in matched] == [100, 102, 103]
    assert matched[1][0] == payloads[2]


@pytest.fixture
def install(tmp_path):
    """Install with Resources.mpk holding an EZST anim and a LuaText script, and an empty patch db."""
    root = tmp_path / "install"
    patch = root / "LocalData" / "Patch"
    patch.mkdir(parents=True)
    anim, script = codecs.ezst_encode(ANIM_DATA), codecs.luatext_encode(CHUNK)
    (root / "Resources.mpk").write_bytes(anim + script)
    write_mpkinfo(root / "Resources.mpkinfo", [(ids.file_id(ANIM), 0, len(anim), 0),
                                               (ids.file_id(SCRIPT), len(anim), len(script), 0)])
    with sqlite3.connect(patch / "Patch.mpkdb") as con:
        con.execute(SCHEMA.format("info0"))
        con.execute(SCHEMA.format("info1"))
    (patch / "Patch5.mpk").write_bytes(b"")
    return root


def files(install: Path, *args: str) -> subprocess.CompletedProcess:
    cmd, rest = args[0], args[1:]
    run = subprocess.run([sys.executable, "-m", "wwm.files", cmd, str(install), *rest,
                          "--backup-dir", str(install.parent / "backups"), "--force"],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return run


def export(install: Path, path: str) -> bytes:
    out = install.parent / "export.bin"
    files(install, "export", path, str(out))
    return out.read_bytes()


def test_override_db_route_and_revert(install):
    assert export(install, ANIM) == ANIM_DATA
    new = install.parent / "new.anim"
    new.write_bytes(b"replacement " * 300)
    files(install, "override", ANIM, str(new))
    assert export(install, ANIM) == ANIM_DATA  # dry run wrote nothing
    files(install, "override", ANIM, str(new), "--apply")
    assert export(install, ANIM) == new.read_bytes()  # patch db row wins over the package
    stored = (install / "LocalData/Patch/Patch5.mpk").read_bytes()
    assert stored[:4] == b"EZST"
    with sqlite3.connect(install / "LocalData/Patch/Patch.mpkdb") as con:
        v3 = con.execute("select v3 from info0 where v0=?", (ids.signed64(ids.file_id(ANIM)),)).fetchone()[0]
    assert v3 == ids.signed64(ids.content_hash(stored))
    files(install, "revert", ANIM)
    assert export(install, ANIM) == ANIM_DATA
    assert (install / "LocalData/Patch/Patch5.mpk").stat().st_size == 0


def test_override_in_place_keeps_size_and_reverts(install):
    before = (install / "Resources.mpk").read_bytes()
    new = install.parent / "new.anim"
    new.write_bytes(b"short")
    files(install, "override", ANIM, str(new), "--in-place", "--apply")
    after = (install / "Resources.mpk").read_bytes()
    assert len(after) == len(before)
    assert export(install, ANIM) == b"short"
    assert export(install, SCRIPT) == CHUNK  # neighbour untouched
    files(install, "revert", ANIM, "--in-place")
    assert (install / "Resources.mpk").read_bytes() == before


def test_lua_source_override_round_trip(install):
    source = install.parent / "strict.lua"
    source.write_bytes(b"print('override')\n")
    files(install, "override", SCRIPT, str(source), "--apply")
    stored = (install / "LocalData/Patch/Patch5.mpk").read_bytes()
    assert codecs.luatext_decode(stored, require_chunk=False) == source.read_bytes()
    assert export(install, SCRIPT) == source.read_bytes()
