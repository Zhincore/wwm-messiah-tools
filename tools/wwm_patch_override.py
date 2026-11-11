#!/usr/bin/env python3
"""
Override one VFS file by adding an entry to the patch database (LocalData/Patch/Patch.mpkdb).

The replacement is appended to a patch pack and a row is inserted into table `info0`
(v0 file id, v1 size, v2 pack, v3 content hash, v4/v5 flags, v6 offset). Base packs are not touched.
Every change is journalled so `--revert` restores the previous state.

Usage:
  wwm_patch_override.py <install_root> <vfs_path> <replacement_file>            dry run
  wwm_patch_override.py <install_root> <vfs_path> <replacement_file> --apply
  wwm_patch_override.py <install_root> <vfs_path> --revert

Add --in-place to overwrite the file's bytes inside its base pack instead (needed for files the
patcher opens before patch entries are registered, e.g. the login video). The replacement must not
be larger than the original; a smaller MP4 is padded with a `free` box. The original bytes are
saved and `--in-place --revert` writes them back.

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

import mmh3

_here = Path(__file__).resolve().parent
sys.path.insert(0, str(_here))
import wwm_harvest_paths as hp  # noqa: E402

MASK64 = (1 << 64) - 1
SMALL_FILE = 4 << 20
CHUNK_LARGE_FILE = 2 << 20
CHUNK_SMALL_FILE = 32 << 20
USER_PACK_LIMIT = 1000
DEFAULT_BACKUP_DIR = Path.home() / ".local/share/wwm-messiah-tools/overrides"


def content_hash(data: bytes) -> int:
    """MurmurHash3-128 per chunk, halves XORed, chunks XORed; files >= 4 MiB use 2 MiB chunks."""
    chunk = CHUNK_LARGE_FILE if len(data) >= SMALL_FILE else CHUNK_SMALL_FILE
    acc = 0
    for i in range(0, max(len(data), 1), chunk):
        h = mmh3.hash128(data[i : i + chunk], signed=False)
        acc ^= (h >> 64) ^ (h & MASK64)
    return acc


def signed64(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


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


def journal_path(backup_dir: Path, file_id: int) -> Path:
    return backup_dir / f"{file_id:016x}.json"


def apply(args: argparse.Namespace, patch_dir: Path, db_path: Path, file_id: int) -> None:
    data = Path(args.replacement).read_bytes()
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as ro:
        pack_no = args.pack if args.pack is not None else pick_pack(patch_dir, ro)
    pack = patch_dir / f"Patch{pack_no}.mpk"
    offset = pack.stat().st_size if pack.exists() else 0
    chash = content_hash(data)
    row = (signed64(file_id), len(data), pack_no, signed64(chash), 0, 0, offset)
    journal = journal_path(args.backup_dir, file_id)

    print(f"file id      {file_id:016x} ({args.vfs_path})")
    print(f"replacement  {args.replacement} ({len(data)} bytes, content hash {chash:016x})")
    print(f"pack         {pack} (append at offset {offset})")
    print(f"info0 row    {row}")
    if journal.exists():
        sys.exit(f"an override for this file is already journalled ({journal}); --revert it first")
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
            sys.exit("verification failed: pack bytes do not hash to the inserted value; run --revert")
    print(f"applied. database backup: {db_backup}\njournal: {journal}")


def revert(args: argparse.Namespace, db_path: Path, file_id: int) -> None:
    journal = journal_path(args.backup_dir, file_id)
    if not journal.exists():
        sys.exit(f"no journal for {file_id:016x} in {args.backup_dir}")
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


def pad_to(data: bytes, size: int, name: str) -> bytes:
    """Grow `data` to exactly `size` bytes; MP4 files get a trailing `free` box."""
    gap = size - len(data)
    if gap < 0:
        sys.exit(f"replacement is {-gap} bytes larger than the original ({size}); in-place needs <= original")
    if gap == 0:
        return data
    if not name.lower().endswith(".mp4") or gap < 8:
        sys.exit(f"cannot pad {name}: {gap} bytes short and no padding rule for this type")
    return data + gap.to_bytes(4, "big") + b"free" + bytes(gap - 8)


def base_entry(install_root: Path, file_id: int):
    wt = hp.load_tool("wwm_textures")
    hits = [e for e in wt.collect_entries(install_root, {"res", "lt", "cache", "db"}) if e.file_id == file_id]
    base = [e for e in hits if e.source == "res"]
    if len(base) != 1:
        sys.exit(f"expected one base-pack entry, found {len(base)} (all sources: {len(hits)})")
    if len(hits) > 1:
        print(f"note: {len(hits) - 1} patch entry(ies) also exist for this file and take precedence once registered")
    return base[0]


def inplace_journal(backup_dir: Path, file_id: int) -> Path:
    return backup_dir / f"{file_id:016x}.inplace.json"


def apply_inplace(args: argparse.Namespace, db_path: Path, file_id: int) -> None:
    entry = base_entry(args.install_root, file_id)
    with open(entry.container, "rb") as f:
        f.seek(entry.offset)
        original = f.read(entry.size)
    if original[:4] in (b"EZST", b"ZSTD"):
        sys.exit("the stored file is an EZST block; in-place replacement of compressed entries is not supported")
    data = pad_to(Path(args.replacement).read_bytes(), entry.size, args.vfs_path)
    journal = inplace_journal(args.backup_dir, file_id)
    print(f"file id      {file_id:016x} ({args.vfs_path})")
    print(f"target       {entry.container} offset {entry.offset} size {entry.size}")
    print(f"original     content hash {content_hash(original):016x}")
    print(f"replacement  {args.replacement} padded to {len(data)} bytes, content hash {content_hash(data):016x}")
    if journal.exists():
        sys.exit(f"an in-place override for this file is already journalled ({journal}); revert it first")
    if not args.apply:
        print("dry run: nothing written (add --apply)")
        return
    refuse_if_in_use(db_path, args.force)
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    saved = args.backup_dir / f"{file_id:016x}.original"
    saved.write_bytes(original)
    journal.write_text(json.dumps({
        "vfs_path": args.vfs_path, "file_id": file_id, "container": entry.container, "offset": entry.offset,
        "size": entry.size, "original": str(saved), "original_hash": content_hash(original),
    }, indent=1))
    with open(entry.container, "r+b") as f:
        f.seek(entry.offset)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    with open(entry.container, "rb") as f:
        f.seek(entry.offset)
        if f.read(entry.size) != data:
            sys.exit("verification failed: pack bytes differ from the replacement; run --in-place --revert")
    print(f"applied in place. original bytes: {saved}\njournal: {journal}")


def revert_inplace(args: argparse.Namespace, db_path: Path, file_id: int) -> None:
    journal = inplace_journal(args.backup_dir, file_id)
    if not journal.exists():
        sys.exit(f"no in-place journal for {file_id:016x} in {args.backup_dir}")
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
    journal.rename(journal.with_suffix(".reverted.json"))
    print(f"restored {j['size']} original bytes at {j['container']}:{j['offset']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root", type=Path)
    ap.add_argument("vfs_path", help="path inside the VFS, e.g. Video/login_loop_201.mp4")
    ap.add_argument("replacement", nargs="?")
    ap.add_argument("--apply", action="store_true", help="write the pack bytes and the database row")
    ap.add_argument("--revert", action="store_true", help="undo a previous --apply for this path")
    ap.add_argument("--in-place", action="store_true", help="overwrite the bytes in the base pack instead")
    ap.add_argument("--pack", type=int, help="patch pack number to append to (default: an empty one)")
    ap.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    ap.add_argument("--force", action="store_true", help="skip the game-running and database-in-use checks")
    args = ap.parse_args()

    patch_dir = args.install_root / "LocalData" / "Patch"
    db_path = patch_dir / "Patch.mpkdb"
    if not db_path.exists():
        sys.exit(f"not found: {db_path}")
    file_id = hp.file_id(args.vfs_path)
    if args.revert:
        (revert_inplace if args.in_place else revert)(args, db_path, file_id)
    elif args.replacement and args.in_place:
        apply_inplace(args, db_path, file_id)
    elif args.replacement:
        apply(args, patch_dir, db_path, file_id)
    else:
        ap.error("need a replacement file or --revert")


if __name__ == "__main__":
    main()
