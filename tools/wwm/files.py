#!/usr/bin/env python3
"""
Override VFS files through the patch database (LocalData/Patch/Patch.mpkdb).

The replacement is appended to a patch pack and a row is inserted into table `info0`
(v0 file id, v1 size, v2 pack, v3 content hash, v4/v5 flags, v6 offset). Base packs are not touched.
Every change is journalled so `revert` restores the previous state.

Usage:
  wwm-files override <install_root> <vfs_path> <replacement_file>          dry run
  wwm-files override <install_root> <vfs_path> <replacement_file> --apply
  wwm-files revert <install_root> <vfs_path>
  wwm-files revert-all <install_root>                                      undo every override, newest first
  wwm-files export <install_root> <vfs_path> <out_file>                    decoded content, as the engine resolves it

Add --in-place to overwrite the file's stored bytes inside its pack instead (needed for files the
patcher opens before patch entries are registered, e.g. the login video). The stored replacement
must not be larger than the original. The original bytes are saved and `revert --in-place`
writes them back. When the overwritten entry is itself a patch-database row (an earlier override),
its `v3` content hash is updated to match the new bytes (and restored on revert) so the row stays
consistent; the database is backed up first.

The replacement file is the *decoded* content. It is stored the way the original entry is:
EZST (zstd + AES prefix) and ZSTD entries are re-compressed, in place to exactly the original
stored size; LuaText entries (u32 size + LZ4 block) are re-wrapped, so a script override may be
plain source or bytecode; raw entries are written as is (a smaller MP4 is padded with a `free` box).
Pass --stored when the replacement file already is the stored form.
<vfs_path> may also be `id:<16 hex digits>` for files known only by id (GUID-keyed textures).

Close the game first: the tool refuses to write while wwm.exe is running here or the database
looks in use (it cannot see a game running in a VM or on another machine that shares the disk).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path


from . import codecs, vfs
from .ids import content_hash, file_id, signed64

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))

USER_PACK_LIMIT = 1000
ZSTD_MAX_LEVEL = 22
DEFAULT_BACKUP_DIR = Path.home() / ".local/share/wwm-messiah-tools/overrides"




def game_running() -> bool:
    """True when a local process was started from a wwm.exe binary."""
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if any(a.replace(b"\\", b"/").rsplit(b"/", 1)[-1].lower() == b"wwm.exe" for a in argv[:3]):
            return True
    return False


def pick_pack(patch_dir: Path, con: sqlite3.Connection) -> int:
    """
    Highest-numbered empty Patch<n>.mpk below the user-content range. When every pack is in use,
    the highest indexed pack whose entries end exactly at its file size (safe to append to).
    """
    sizes = {
        int(m.group(1)): (patch_dir / f).stat().st_size
        for f in os.listdir(patch_dir)
        if (m := re.fullmatch(r"Patch(\d+)\.mpk", f))
    }
    empty = [n for n, size in sizes.items() if size == 0 and n < USER_PACK_LIMIT]
    if empty:
        return max(empty)
    ends = con.execute(
        "select v2, max(v6 + v1) from (select v1, v2, v6 from info0 union all select v1, v2, v6 from info1) "
        "where v2 < ? group by v2 order by v2 desc", (USER_PACK_LIMIT,)
    )
    for pack_no, end in ends:
        if sizes.get(pack_no) == end:
            return pack_no
    sys.exit("no suitable patch pack found; pass --pack")


def database_busy(db_path: Path) -> str | None:
    """Reason to believe another process has the database open, else None."""
    wal = Path(f"{db_path}-wal")
    if wal.exists() and wal.stat().st_size:
        return f"{wal.name} is not empty"
    con = sqlite3.connect(db_path, timeout=0)
    try:
        con.execute("begin exclusive")
        con.execute("rollback")
    except sqlite3.OperationalError as e:
        return str(e)
    finally:
        con.close()
    return None


def refuse_if_in_use(db_path: Path, force: bool) -> None:
    if force:
        return
    if game_running():
        sys.exit("wwm.exe is running; close the game first")
    reason = database_busy(db_path)
    if reason:
        sys.exit(f"patch database looks in use ({reason}); close the game, or pass --force if it is closed")


INFO1_XOR_KEY = 1234927562014348681


def read_info0_flags(db_path: Path, file_id: int) -> tuple[int, int] | None:
    """(v4, v5) of the existing info0 row for this id, or None. v5 is the patcher-file flag."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as ro:
        r = ro.execute("select v4, v5 from info0 where v0=?", (signed64(file_id),)).fetchone()
    return (r[0], r[1]) if r else None


def db_has_info1(db_path: Path, file_id: int) -> bool:
    """Whether a background (info1) row exists; its key is the file id XORed with INFO1_XOR_KEY."""
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as ro:
        r = ro.execute("select 1 from info1 where v0=?", (signed64(file_id ^ INFO1_XOR_KEY),)).fetchone()
    return r is not None


def journal_path(backup_dir: Path, file_id: int) -> Path:
    return backup_dir / f"{file_id:016x}.json"


def apply(args: argparse.Namespace, patch_dir: Path, db_path: Path, file_id: int) -> None:
    existing, n_hits = find_entries(args.install_root, file_id)
    flags = read_info0_flags(db_path, file_id)
    if not existing and n_hits == 0 and flags is None and not db_has_info1(db_path, file_id) and not args.new:
        sys.exit(
            "file id is not in any package index or the patch db; the engine would never query it.\n"
            "  - pass the engine's real id as id:<16hex> (path hashing can miss reconstructed names), or\n"
            "  - pass --new to add a genuinely new file."
        )
    wrapper = wrapper_of(read_stored(existing[0])) if existing else None
    if not existing:
        print("note: no physical package copy found; storing the replacement raw")
    data = stored_form(args, wrapper, None)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as ro:
        pack_no = args.pack if args.pack is not None else pick_pack(patch_dir, ro)
    pack = patch_dir / f"Patch{pack_no}.mpk"
    offset = pack.stat().st_size if pack.exists() else 0
    chash = content_hash(data)
    v4, v5 = flags if flags else (0, 0)
    if args.v5 is not None:
        v5 = args.v5
    row = (signed64(file_id), len(data), pack_no, signed64(chash), v4, v5, offset)
    journal = journal_path(args.backup_dir, file_id)

    print(f"file id      {file_id:016x} ({args.vfs_path})")
    print(f"replacement  {args.replacement} stored {(wrapper or b'raw').decode()}, {len(data)} bytes, content hash {chash:016x}")
    if v5 == 0 and flags is None and args.v5 is None:
        print("note: patcher flag v5=0; bootstrap/patcher scripts (patch_main, patcher, launcher, ...) need --v5 1")
    if v5 == 1:
        print("warning: v5=1 (patcher-class) row; the boot patcher/essential verify drops such overrides, "
              "so it will not survive a launch (docs/patching.md)")
    print(f"pack         {pack} (append at offset {offset})")
    print(f"info0 row    {row}")
    if journal.exists():
        sys.exit(f"an override for this file is already journalled ({journal}); `wwm-files revert` it first")
    if not args.apply:
        print("dry run: nothing written (add --apply)")
        return
    refuse_if_in_use(db_path, args.force)

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    db_backup = args.backup_dir / f"Patch.mpkdb.{time.strftime('%Y%m%d-%H%M%S')}"
    con = sqlite3.connect(db_path)
    try:
        with sqlite3.connect(db_backup) as dst:
            con.backup(dst)
        previous = {t: con.execute(f"select * from {t} where v0=?", (row[0],)).fetchall() for t in ("info0", "info1")}
        journal.write_text(json.dumps({
            "vfs_path": args.vfs_path, "file_id": file_id, "row": row, "pack": str(pack),
            "pack_size_before": offset, "pack_existed": pack.exists(), "previous": previous,
            "db_backup": str(db_backup),
        }, indent=1))
        with open(pack, "ab") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with con:
            con.execute("delete from info1 where v0=?", (row[0],))
            con.execute("insert or replace into info0 values (?,?,?,?,?,?,?)", row)
    finally:
        con.close()

    with open(pack, "rb") as f:
        f.seek(offset)
        if content_hash(f.read(len(data))) != chash:
            sys.exit("verification failed: pack bytes do not hash to the inserted value; run `wwm-files revert`")
    print(f"applied. database backup: {db_backup}\njournal: {journal}")


def revert(args: argparse.Namespace, db_path: Path, journal: Path) -> None:
    if not journal.exists():
        sys.exit(f"no journal {journal}")
    j = json.loads(journal.read_text())
    refuse_if_in_use(db_path, args.force)
    row, pack = j["row"], Path(j["pack"])
    con = sqlite3.connect(db_path)
    try:
        with con:
            current = con.execute("select * from info0 where v0=?", (row[0],)).fetchone()
            if current is not None and list(current) != row:
                print(f"info0 row changed since apply (now {current}); leaving the database row as is")
            else:
                con.execute("delete from info0 where v0=?", (row[0],))
                for table, rows in j["previous"].items():
                    con.executemany(f"insert or replace into {table} values (?,?,?,?,?,?,?)", rows)
    finally:
        con.close()
    end = row[6] + row[1]
    if pack.exists() and pack.stat().st_size == end:
        with open(pack, "r+b") as f:
            f.truncate(j["pack_size_before"])
        print(f"truncated {pack} to {j['pack_size_before']} bytes")
    elif pack.exists():
        print(f"{pack} grew since apply ({pack.stat().st_size} != {end}); left untouched")
    journal.rename(journal.with_suffix(".reverted.json"))
    print(f"reverted. full database backup from before apply: {j['db_backup']}")


WRAPPERS = (b"EZST", b"ZSTD")
LUATEXT = b"LuaText"
SOURCE_PRIORITY = {"db1": 0, "db0": 1, "res": 2, "lt": 2, "cache": 3}


def parse_file_id(vfs_path: str) -> int:
    m = re.fullmatch(r"id:([0-9a-fA-F]{16})", vfs_path)
    return int(m.group(1), 16) if m else file_id(vfs_path)


def pad_raw(data: bytes, size: int, name: str) -> bytes:
    """Grow raw `data` to exactly `size` bytes; MP4 files get a trailing `free` box."""
    gap = size - len(data)
    if gap == 0:
        return data
    if gap < 0:
        sys.exit(f"replacement is {-gap} bytes larger than the original ({size}); in-place needs <= original")
    if not name.lower().endswith(".mp4") or gap < 8:
        sys.exit(f"cannot pad {name}: {gap} bytes short and no padding rule for this type")
    return data + gap.to_bytes(4, "big") + b"free" + bytes(gap - 8)


def stored_form(args: argparse.Namespace, wrapper: bytes | None, size: int | None) -> bytes:
    """Replacement bytes as they go into the pack: wrapped like the original, `size` bytes if given."""
    data = Path(args.replacement).read_bytes()
    if wrapper == LUATEXT and not args.stored:
        block = codecs.luatext_encode(data)
        if size is not None and len(block) != size:
            sys.exit(f"LuaText replacement is {len(block)} bytes stored, original {size}; "
                     "LZ4 cannot be padded to size, use the database route")
        return block
    if args.stored or wrapper not in WRAPPERS:
        return data if size is None else pad_raw(data, size, args.vfs_path)
    block, error = None, None
    for level in dict.fromkeys((args.level, ZSTD_MAX_LEVEL)):
        try:
            block = codecs.ezst_encode(data, wrapper, size, level)
            break
        except ValueError as e:
            error = e
    if block is None:
        sys.exit(f"{error}; the compressed replacement does not fit the original stored size")
    if codecs.ezst_decode(block) != data:
        sys.exit("internal error: re-wrapped block does not decode back to the replacement")
    return block


def find_entries(install_root: Path, file_id: int) -> tuple[list, int]:
    """Entries with physical offsets (engine-preferred first) and the raw hit count across all sources.

    Block packs (EZST) index files by logical block offsets, so their entries cannot be seeked to a
    byte position; they are dropped here. A nonzero raw count with an empty list means the file exists
    only in block packs: patch it through the database route, not in place.
    """
    hits = [e for e in vfs.collect_entries(install_root, {"res", "lt", "cache", "db"}) if e.file_id == file_id]
    usable = []
    for e in hits:
        if e.source == "cache" and not os.path.exists(e.container):
            continue
        if e.source in ("res", "lt", "cache") and vfs.Pack(e.container).blocks is not None:
            print(f"note: skipping {e.container} ({e.source} block pack; index offset is not a physical file offset)")
            continue
        usable.append(e)
    usable.sort(key=lambda e: SOURCE_PRIORITY.get(e.source, 9))
    return usable, len(hits)


def read_stored(entry) -> bytes:
    with open(entry.container, "rb") as f:
        f.seek(entry.offset)
        return f.read(entry.size)


def wrapper_of(stored: bytes) -> bytes | None:
    if stored[:4] in WRAPPERS:
        return stored[:4]
    return LUATEXT if codecs.luatext_decode(stored) is not None else None


def export(args: argparse.Namespace, file_id: int) -> None:
    """Write the decoded content of the entry the engine resolves first (patch db before packages)."""
    entries, n_hits = find_entries(args.install_root, file_id)
    if args.source:
        entries = [e for e in entries if e.source == args.source]
    if not entries:
        if n_hits:
            sys.exit("this file lives only in block (EZST) packs with no physical offset; cannot export it")
        sys.exit("no index entry found for this file (check the id; path hashing can miss reconstructed names)")
    entry = entries[0]
    stored = read_stored(entry)
    for other in entries[1:]:
        if other.source != entry.source and read_stored(other) != stored:
            print(f"note: {other.container} ({other.source}) holds different bytes; pick it with --source {other.source}")
    wrapper = wrapper_of(stored)
    if args.stored or wrapper is None:
        data = stored
    elif wrapper == LUATEXT:
        data = codecs.luatext_decode(stored)
    else:
        data = codecs.ezst_decode(stored)
    Path(args.output).write_bytes(data)
    form = "stored" if args.stored else (wrapper or b"raw").decode()
    print(f"file id      {file_id:016x} ({args.vfs_path})")
    print(f"source       {entry.container} ({entry.source}) offset {entry.offset} size {entry.size}")
    print(f"wrote        {args.output}: {len(data)} bytes ({form}{', decoded' if wrapper and not args.stored else ''})")
    if wrapper == LUATEXT and not args.stored:
        print("note: Lua bytecode in the game's opcode numbering; `wwm-lua` remaps to stock 5.4 for decompiling")


def inplace_journal(backup_dir: Path, file_id: int) -> Path:
    return backup_dir / f"{file_id:016x}.inplace.json"


def db_target(source: str, file_id: int) -> tuple[str, int] | None:
    """(table, v0 key) of the row behind a db-sourced entry; None for base-pack sources."""
    if source == "db0":
        return "info0", signed64(file_id)
    if source == "db1":
        return "info1", signed64(file_id ^ INFO1_XOR_KEY)
    return None


def apply_inplace(args: argparse.Namespace, db_path: Path, file_id: int) -> None:
    entries, n_hits = find_entries(args.install_root, file_id)
    if args.source:
        entries = [e for e in entries if e.source == args.source]
    if not entries:
        if n_hits:
            sys.exit("this file lives only in block (EZST) packs with no physical offset; use the database route instead")
        sys.exit("no index entry found for this file (check the id; path hashing can miss reconstructed names)")
    entry = entries[0]
    for other in entries[1:]:
        print(f"note: another entry exists in {other.container} ({other.source}); pick it with --source {other.source}")
    original = read_stored(entry)
    data = stored_form(args, wrapper_of(original), entry.size)
    new_hash = content_hash(data)
    target = db_target(entry.source, file_id)
    journal = inplace_journal(args.backup_dir, file_id)
    print(f"file id      {file_id:016x} ({args.vfs_path})")
    print(f"target       {entry.container} offset {entry.offset} size {entry.size}")
    print(f"original     content hash {content_hash(original):016x}")
    print(f"stored as    {(wrapper_of(original) or b'raw').decode()} ({entry.source})")
    print(f"replacement  {args.replacement} stored as {len(data)} bytes, content hash {new_hash:016x}")
    if target:
        print(f"db row       {target[0]} v3 content hash -> {new_hash:016x} (kept in sync with the overwritten bytes)")
    flags = read_info0_flags(db_path, file_id)
    if flags and flags[1] == 1:
        print("warning: this file has a v5=1 (patcher-class) info0 row; the boot patcher/essential verify "
              "may restore it from the server, so an in-place edit can be undone on launch (docs/patching.md)")
    if journal.exists():
        sys.exit(f"an in-place override for this file is already journalled ({journal}); revert it first")
    if not args.apply:
        print("dry run: nothing written (add --apply)")
        return
    refuse_if_in_use(db_path, args.force)
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    saved = args.backup_dir / f"{file_id:016x}.original"
    saved.write_bytes(original)
    record = {
        "vfs_path": args.vfs_path, "file_id": file_id, "container": entry.container, "offset": entry.offset,
        "size": entry.size, "original": str(saved), "original_hash": content_hash(original),
    }
    con = sqlite3.connect(db_path) if target else None
    try:
        if target:
            table, key = target
            db_backup = args.backup_dir / f"Patch.mpkdb.{time.strftime('%Y%m%d-%H%M%S')}"
            with sqlite3.connect(db_backup) as dst:
                con.backup(dst)
            row_before = con.execute(f"select * from {table} where v0=?", (key,)).fetchone()
            if row_before is None:
                sys.exit(f"{table} row for the target entry vanished; aborting without writing")
            record.update(db_table=table, db_key=key, db_row_before=list(row_before),
                          db_v3_applied=signed64(new_hash), db_backup=str(db_backup))
        journal.write_text(json.dumps(record, indent=1))
        with open(entry.container, "r+b") as f:
            f.seek(entry.offset)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with open(entry.container, "rb") as f:
            f.seek(entry.offset)
            if f.read(entry.size) != data:
                sys.exit("verification failed: pack bytes differ from the replacement; run `wwm-files revert --in-place`")
        if target:
            with con:
                con.execute(f"update {table} set v3=? where v0=?", (signed64(new_hash), key))
    finally:
        if con:
            con.close()
    print(f"applied in place. original bytes: {saved}\njournal: {journal}")
    if target:
        print(f"database backup: {record['db_backup']}")


def revert_inplace(args: argparse.Namespace, db_path: Path, journal: Path) -> None:
    if not journal.exists():
        sys.exit(f"no in-place journal {journal}")
    j = json.loads(journal.read_text())
    refuse_if_in_use(db_path, args.force)
    original = Path(j["original"]).read_bytes()
    if len(original) != j["size"] or content_hash(original) != j["original_hash"]:
        sys.exit("saved original does not match the journal; not writing")
    with open(j["container"], "r+b") as f:
        f.seek(j["offset"])
        f.write(original)
        f.flush()
        os.fsync(f.fileno())
    print(f"restored {j['size']} original bytes at {j['container']}:{j['offset']}")
    if "db_table" in j:
        table, key, before = j["db_table"], j["db_key"], j["db_row_before"]
        con = sqlite3.connect(db_path)
        try:
            with con:
                current = con.execute(f"select * from {table} where v0=?", (key,)).fetchone()
                if current is not None and current[3] != j["db_v3_applied"]:
                    print(f"{table} v3 changed since apply (now {current[3]}); leaving the database row as is")
                else:
                    con.execute(f"update {table} set v3=? where v0=?", (before[3], key))
                    print(f"restored {table} v3 content hash")
        finally:
            con.close()
    journal.rename(journal.with_suffix(".reverted.json"))


def revert_all(args: argparse.Namespace, db_path: Path) -> None:
    """Revert every active journal that targets this install, newest first so pack tails unwind in order."""
    root = args.install_root.resolve()
    active = []
    for journal in args.backup_dir.glob("*.json"):
        if journal.name.endswith(".reverted.json"):
            continue
        j = json.loads(journal.read_text())
        if Path(j.get("container") or j["pack"]).resolve().is_relative_to(root):
            active.append((journal.stat().st_mtime, journal, j))
    if not active:
        print(f"no active overrides for {root} in {args.backup_dir}")
        return
    refuse_if_in_use(db_path, args.force)
    for _, journal, j in sorted(active, key=lambda a: a[0], reverse=True):
        in_place = "container" in j
        print(f"-- {j['vfs_path']} ({'in place' if in_place else 'database'})")
        (revert_inplace if in_place else revert)(args, db_path, journal)
    print(f"reverted {len(active)} override(s)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("install_root", type=Path)
    common.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    common.add_argument("--force", action="store_true", help="skip the game-running and database-in-use checks")
    sub = ap.add_subparsers(dest="cmd", required=True)

    o = sub.add_parser("override", parents=[common], help="replace one file (dry run unless --apply)")
    o.add_argument("vfs_path", help="path inside the VFS, e.g. Video/login_loop_201.mp4, or id:<16 hex>")
    o.add_argument("replacement", help="decoded replacement content (see --stored)")
    o.add_argument("--apply", action="store_true", help="write the pack bytes and the database row")
    o.add_argument("--in-place", action="store_true", help="overwrite the bytes in the base pack instead")
    o.add_argument("--source", choices=sorted(SOURCE_PRIORITY), help="index source of the entry to overwrite in place")
    o.add_argument("--stored", action="store_true", help="replacement file is already in stored form; write it as is")
    o.add_argument("--v5", type=int, choices=(0, 1), help="info0 patcher-file flag for the new row (default: keep existing, else 0)")
    o.add_argument("--new", action="store_true", help="allow a file id absent from every index and the patch db")
    o.add_argument("--level", type=int, default=19, help="zstd level for re-compressed entries")
    o.add_argument("--pack", type=int, help="patch pack number to append to (default: an empty one)")

    r = sub.add_parser("revert", parents=[common], help="undo an override of one file")
    r.add_argument("vfs_path")
    r.add_argument("--in-place", action="store_true", help="undo an in-place override")

    sub.add_parser("revert-all", parents=[common], help="undo every journalled override of this install")

    x = sub.add_parser("export", parents=[common], help="write a file's decoded content (read-only)")
    x.add_argument("vfs_path", help="path inside the VFS, or id:<16 hex>")
    x.add_argument("output", help="file to write")
    x.add_argument("--source", choices=sorted(SOURCE_PRIORITY), help="export the copy from this index source")
    x.add_argument("--stored", action="store_true", help="write the stored form (still wrapped) instead")
    args = ap.parse_args()

    patch_dir = args.install_root / "LocalData" / "Patch"
    db_path = patch_dir / "Patch.mpkdb"
    if not db_path.exists():
        sys.exit(f"not found: {db_path}")
    if args.cmd == "revert-all":
        return revert_all(args, db_path)
    file_id = parse_file_id(args.vfs_path)
    if args.cmd == "export":
        return export(args, file_id)
    if args.cmd == "revert":
        if args.in_place:
            revert_inplace(args, db_path, inplace_journal(args.backup_dir, file_id))
        else:
            revert(args, db_path, journal_path(args.backup_dir, file_id))
    elif args.in_place:
        apply_inplace(args, db_path, file_id)
    else:
        apply(args, patch_dir, db_path, file_id)


if __name__ == "__main__":
    main()
