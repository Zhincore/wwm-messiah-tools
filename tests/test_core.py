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


# ---- gamedata export ----

def test_gamedata_export_kind():
    import types
    from wwm import gamedata
    rows = [
        {"_key": 10102, "kongfu_id": 10102, "name": "Nameless Sword", "weapon_type": 101, "hide": None},
        {"_key": 5, "kongfu_id": 5, "name": ""},          # unnamed -> skipped by default
        {"_key": 7, "kongfu_id": 7, "name": "Other", "weapon_type": 2},
    ]
    store = types.SimpleNamespace(manifest=types.SimpleNamespace(tables={"kongfu": 1}),
                                  table=lambda name: {"rows": rows})
    data, count, named = gamedata.export_kind(store, "kongfu", "kongfu_id", "name", ["weapon_type", "hide"], False)
    assert count == 2 and named == 2
    assert data["10102"] == {"id": 10102, "name": "Nameless Sword", "weapon_type": 101}  # None 'hide' dropped
    assert "5" not in data
    # missing table -> None
    assert gamedata.export_kind(store, "nope", "id", "name", [], False)[0] is None
    # keep_unnamed includes the blank one with name=None
    data2, count2, named2 = gamedata.export_kind(store, "kongfu", "kongfu_id", "name", [], True)
    assert count2 == 3 and named2 == 2 and data2["5"]["name"] is None


# ---- tuning export ----

def test_tuning_build():
    import types
    from wwm import tuning
    tables = {
        "formula_base_attrs": [{"_key": 329, "prop_name_en": "CRI_PROB", "prop_name_cn": "Critical Rate"}],
        "equip_affix_config": [
            {"_key": 1, "affix_para": 329, "affix_des": "", "random_weight": 930, "min_val": 0.052, "max_val": 0.104},
            {"_key": 2, "affix_para": -1, "affix_des": "Restores HP.", "random_weight": 6},
        ],
        "equip_toning_group": [{"_key": 100930, "1": 930, "2": 6}],
        "equip_determin_group": [{"_key": 908, "det_group": 1907, "type": 1, "cost": [2200028, 1]}],
        "equip_attr_cols": [{"_key": 1, "base": ["MAX_W_ATK"]}],
        "equip_items": [
            {"_key": 11, "name": "Sword", "re_group": 100930, "det_group_21": 909, "det_group_1": 908,
             "MAX_W_ATK": [[175, 175, 100]], "icon_no": "x.png", "re_reset_cost": [2233604, 2]},
            {"_key": 12, "name": "Fishing Rod"},          # rolls nothing -> left out
        ],
        "stuff_items": [{"_key": 2233604, "name": "Modulating Stone"}, {"_key": 2200028, "name": "Attunement Stone"}],
        "neigong_liupai_data": [{"_key": 3, "name": "Silkbind - Jade"}],
    }
    store = types.SimpleNamespace(manifest=types.SimpleNamespace(tables=tables),
                                  table=lambda name: {"rows": tables[name]})
    data, missing = tuning.build(store)
    assert data["affixes"][1]["name"] == "Critical Rate" and data["affixes"][1]["attr"] == "CRI_PROB"
    assert data["affixes"][2]["name"] == "Restores HP." and data["affixes"][2]["attr"] is None
    assert data["pools"][100930] == {"1": 930, "2": 6}
    assert data["items"] == {11: {"name": "Sword", "re_group": 100930, "re_reset_cost": [2233604, 2],
                                  "det_groups": {1: 908, 21: 909}, "base_attrs": {"MAX_W_ATK": [[175, 175, 100]]}}}
    assert data["materials"]["names"] == {2200028: "Attunement Stone", 2233604: "Modulating Stone"}
    assert data["paths"] == {3: "Silkbind - Jade"}
    assert "equip_toning_mode" in missing and "equip_items" not in missing


# ---- dataset export ----

def test_datasets_select_and_export(tmp_path):
    import json
    import types
    from wwm import datasets
    names = ["gacha_pool_common", "gacha_pool_common__grey", "activity_gacha_x", "maps.gacha_room", "rewards", "reward_x", "rewarding"]
    assert datasets.select(names, datasets.DATASETS["gacha"][1]) == ["activity_gacha_x", "gacha_pool_common"]
    assert datasets.select(names, datasets.DATASETS["rewards"][1]) == ["reward_x", "rewards"]
    tables = {
        "plain": {"errors": 1, "rows": [{"_key": 2, "a": 1}, {"_key": 1, "value": 40}]},
        "repeat": {"errors": 0, "rows": [{"_key": 7, "a": 1}, {"_key": 7, "a": 2}, {"_key": 8, "a": 3}]},
    }
    store = types.SimpleNamespace(table=lambda name: tables[name])
    done = datasets.export(store, tmp_path, ["plain", "repeat"])
    assert done == {"plain": {"rows": 2, "errors": 1}, "repeat": {"rows": 3, "errors": 0, "multi": True}}
    assert json.loads((tmp_path / "plain.json").read_text()) == {"2": {"a": 1}, "1": 40}      # bare value kept bare
    assert json.loads((tmp_path / "repeat.json").read_text()) == {"7": [{"a": 1}, {"a": 2}], "8": [{"a": 3}]}


# ---- formula files ----

def _ft(names, formulas, inputs, nodes):
    """Build a formula file: nodes are (kind, value, [u16 fields])."""
    import struct
    name_blob = b"".join(n.encode() + b"\0" for n in names)
    table = struct.pack(f"<{1 + 2 * len(formulas)}H", len(formulas), *[x for pair in formulas for x in pair])
    deps = struct.pack("<H", len(inputs)) + b"".join(
        struct.pack(f"<{3 + len(d)}H", name, node, len(d), *d) for name, node, d in inputs)
    body = struct.pack("<H", len(nodes))
    for i, (kind, value, args) in enumerate(nodes):
        payload = struct.pack("<f", value) + struct.pack(f"<{len(args)}H", *args)
        body += struct.pack("<IHH", 4 + len(payload), kind, i) + payload
    a = 24 + len(name_blob)
    return struct.pack("<6I", 1, 0, 24, a, a + len(table), a + len(table) + len(deps)) + name_blob + table + deps + body


def test_formula_parse_text_evaluate():
    from wwm import formula as fm
    names = ["BASE", "CRIT", "EQUIP", "RATE", "config", "min", "tbl"]
    nodes = [
        (fm.VAR, 2.0, [2, fm.NO_NODE]),             # 0 EQUIP (input)
        (fm.STRING, 0.0, [6]),                      # 1 'tbl'
        (fm.CALL, 1.0, [4, 2, 1, 0]),               # 2 config('tbl', EQUIP)
        (fm.VAR, 1.0, [0, 2]),                      # 3 BASE = node 2
        (fm.CONST, 0.5, []),                        # 4 0.5
        (fm.CHAIN, 1.5, [5, 3, 0, 0, 2, 4]),        # 5 (BASE + EQUIP * 0.5), left to right: (1 + 2) * 0.5
        (fm.CONST, 0.8, []),                        # 6 0.8
        (fm.CALL, 0.8, [5, 2, 5, 6]),               # 7 min(node 5, 0.8)
        (fm.NEG, -0.8, [1, 7]),                     # 8 -node 7
    ]
    data = _ft(names, [(0, 2), (1, 7), (3, 8)], [(2, 0, [5, 7])], nodes)
    f = fm.parse(data)
    assert f.formulas == {"BASE": 2, "CRIT": 7, "RATE": 8} and f.inputs == {"EQUIP": (0, [5, 7])}
    assert fm.text(f, 7) == "min((BASE + EQUIP * 0.5), 0.8)" and fm.text(f, 8) == "-min((BASE + EQUIP * 0.5), 0.8)"
    assert fm.text(f, 2) == "config('tbl', EQUIP)" and fm.uses(f, 7) == ["BASE", "EQUIP"]
    assert fm.check(f) == []                                             # stored values reproduce
    got = fm.evaluate(f, {"EQUIP": 0.2}, config=lambda args: 0.1 if args == ["tbl", 0.2] else None)
    assert abs(got["CRIT"] - 0.15) < 1e-6 and abs(got["RATE"] + 0.15) < 1e-6 and got["BASE"] == 0.1
    assert fm.describe(f)["inputs"]["EQUIP"] == {"default": 2.0, "feeds": ["CRIT"]}
