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


# ---- HD store in-place field patch ----

def test_string_cell_span_locates_value():
    from wwm import tables
    # row: u16 count=2; cell0 (fid0, u8=7); cell1 (fid1, str "ABCDE")
    row = struct.pack("<H", 2) + bytes([0, 0x04, 7]) + bytes([1, 0x20]) + struct.pack("<H", 5) + b"ABCDE"
    s, e = tables.string_cell_span(row, ["a", "exec"], "exec")
    assert row[s:e] == b"ABCDE"
    with pytest.raises(ValueError):
        tables.string_cell_span(row, ["a", "exec"], "missing")


def test_hd_container_rewrite_roundtrip(tmp_path):
    """read_container/write_container rebuild a multi-block container; a block may change size."""
    from wwm import tables
    payloads = [b"block0", random.Random(3).randbytes(400), b"tail-block"]
    blocks = [bytes(9) + codecs.zstd_frame(p) for p in payloads]
    tables.write_container(tmp_path / "data0", (tables.HD_MAGIC, 1, 0), blocks)
    c = tables.Container(tmp_path / "data0")
    assert [c.block(i) for i in range(3)] == payloads
    # edit the middle block to a DIFFERENT compressed size; other blocks must stay readable
    hdr, raw = tables.read_container(tmp_path / "data0")
    raw[1] = bytes(9) + codecs.zstd_frame(b"replacement-" + b"Z" * 900)
    tables.write_container(tmp_path / "data0", hdr, raw)
    c2 = tables.Container(tmp_path / "data0")
    assert c2.block(0) == payloads[0] and c2.block(2) == payloads[2]
    assert c2.block(1) == b"replacement-" + b"Z" * 900


# ---- LocalDB hotfix patch (auto-run loader) ----

def test_hotfix_patch_prepend_and_revert(tmp_path):
    import types
    import msgpack
    from wwm import decode_text as dt

    db = tmp_path / "db_mp"
    con = sqlite3.connect(db)
    con.execute("create table localdb(id TEXT PRIMARY KEY, key TEXT, scope TEXT, content BLOB)")
    code = "return require('x')"
    enc = lambda v: dt.xor_repeat(msgpack.packb(v), dt.HEX_XOR_KEY)
    con.execute("insert into localdb values(?,?,?,?)", ("hotfix_1global", "hotfix_1", "global", enc(code)))
    con.execute("insert into localdb values(?,?,?,?)", ("hotfix_metaglobal", "hotfix_meta", "global", enc({"version": "1"})))
    con.commit(); con.close()

    ns = lambda **k: types.SimpleNamespace(**k)
    rc = dt.patch_hotfix(ns(patch_hotfix=str(db), all=False, loader=None, apply=True,
                            force=True, journal_dir=str(tmp_path / "j")))
    assert rc == 0
    con = sqlite3.connect(db)
    val = msgpack.unpackb(dt.xor_repeat(con.execute("select content from localdb where key='hotfix_1'").fetchone()[0],
                                        dt.HEX_XOR_KEY), raw=False)
    con.close()
    assert "s.lua" in val and val.endswith(code)  # loader prepended, original kept

    jp = next((tmp_path / "j").glob("*.json"))
    assert dt.revert_hotfix(ns(revert_hotfix=str(jp))) == 0
    con = sqlite3.connect(db)
    back = msgpack.unpackb(dt.xor_repeat(con.execute("select content from localdb where key='hotfix_1'").fetchone()[0],
                                         dt.HEX_XOR_KEY), raw=False)
    con.close()
    assert back == code
