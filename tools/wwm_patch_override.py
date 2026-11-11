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

Close the game first: the tool refuses to write while wwm.exe is running.
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
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if b"wwm.exe" in Path(f"/proc/{pid}/cmdline").read_bytes():
                return True
        except OSError:
            continue
    return False


def pick_pack(patch_dir: Path) -> int:
    """Highest-numbered empty Patch<n>.mpk below the user-content range."""
    empty = [
        int(m.group(1))
        for f in os.listdir(patch_dir)
        if (m := re.fullmatch(r"Patch(\d+)\.mpk", f)) and (patch_dir / f).stat().st_size == 0
    ]
    empty = [n for n in empty if n < USER_PACK_LIMIT]
    if not empty:
        sys.exit("no empty patch pack found; pass --pack")
    return max(empty)


def journal_path(backup_dir: Path, file_id: int) -> Path:
    return backup_dir / f"{file_id:016x}.json"


def apply(args: argparse.Namespace, patch_dir: Path, db_path: Path, file_id: int) -> None:
    data = Path(args.replacement).read_bytes()
    pack_no = args.pack if args.pack is not None else pick_pack(patch_dir)
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
    if game_running() and not args.force:
        sys.exit("wwm.exe is running; close the game first")

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
    if game_running() and not args.force:
        sys.exit("wwm.exe is running; close the game first")
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root", type=Path)
    ap.add_argument("vfs_path", help="path inside the VFS, e.g. Video/login_loop_201.mp4")
    ap.add_argument("replacement", nargs="?")
    ap.add_argument("--apply", action="store_true", help="write the pack bytes and the database row")
    ap.add_argument("--revert", action="store_true", help="undo a previous --apply for this path")
    ap.add_argument("--pack", type=int, help="patch pack number to append to (default: an empty one)")
    ap.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    ap.add_argument("--force", action="store_true", help="write even though wwm.exe is running")
    args = ap.parse_args()

    patch_dir = args.install_root / "LocalData" / "Patch"
    db_path = patch_dir / "Patch.mpkdb"
    if not db_path.exists():
        sys.exit(f"not found: {db_path}")
    file_id = hp.file_id(args.vfs_path)
    if args.revert:
        revert(args, db_path, file_id)
    elif args.replacement:
        apply(args, patch_dir, db_path, file_id)
    else:
        ap.error("need a replacement file or --revert")


if __name__ == "__main__":
    main()
