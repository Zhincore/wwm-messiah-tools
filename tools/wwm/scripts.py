#!/usr/bin/env python3
"""
Run your own Lua files inside the game client: installs a loader and manages the scripts it runs.

`install` puts a loader at <game>/LocalData/userscripts/loader.lua and prepends one line to every cached hotfix
in LocalData/LocalDB/db_mp, so the game runs the loader at startup. The loader runs each enabled script
(LocalData/userscripts/<name>.lua) once per session; docs/scripts.md describes what a script gets.

Only when the user asks, game closed. The hotfix cache needs one earlier login, and the server can
replace cached hotfixes at any time: when `status` shows unpatched rows, run `install` again.

Usage:
  wwm-scripts <game_dir> status
  wwm-scripts <game_dir> install | uninstall          # uninstall restores the hotfix rows; script files stay
  wwm-scripts <game_dir> add FILE.lua ...             # copy into the scripts folder and enable
  wwm-scripts <game_dir> enable|disable|remove NAME ...
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import msgpack

from .decode_text import HEX_XOR_KEY, xor_repeat
from .files import game_running

LOADER_SOURCE = Path(__file__).parent / "script_loader.lua"
LOADER = "loader.lua"
FOLDER = "userscripts"
BOOT = 'pcall(function() load(MPatch.GetRawDataInSystem("LocalData","userscripts/loader.lua"))() end)'
HOTFIX_ROWS = "select key, scope, content from localdb where key like 'hotfix\\_%' escape '\\' and key != 'hotfix_meta'"


class Install:
    """Paths of one game install."""

    def __init__(self, game_dir: Path):
        self.local = game_dir / "LocalData"
        if not self.local.is_dir():
            sys.exit(f"{game_dir}: no LocalData folder; pass the game's install folder")
        self.dir = self.local / FOLDER
        self.db = self.local / "LocalDB" / "db_mp"
        self.journal = self.dir / "journal.json"
        self.enabled_file = self.dir / "enabled.txt"

    def enabled(self) -> list[str]:
        if not self.enabled_file.exists():
            return []
        return [n for n in self.enabled_file.read_text(encoding="utf-8").split() if n]

    def set_enabled(self, names: list[str]) -> None:
        self.dir.mkdir(exist_ok=True)
        self.enabled_file.write_text("".join(f"{n}\n" for n in names), encoding="utf-8")

    def available(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.lua") if p.name != LOADER)


def decode(content: bytes) -> str | None:
    """Code string of one cached hotfix row, or None when the row holds something else."""
    try:
        code = msgpack.unpackb(xor_repeat(content, HEX_XOR_KEY), raw=False, strict_map_key=False)
    except Exception:
        return None
    return code if isinstance(code, str) else None


def encode(code: str) -> bytes:
    return xor_repeat(msgpack.packb(code), HEX_XOR_KEY)


def hotfix_state(db: Path) -> tuple[int, int]:
    """(rows holding code, rows that already carry the boot line); (0, 0) without a database."""
    if not db.is_file():
        return 0, 0
    con = sqlite3.connect(db)
    try:
        codes = [decode(c) for _, _, c in con.execute(HOTFIX_ROWS)]
    finally:
        con.close()
    codes = [c for c in codes if c is not None]
    return len(codes), sum(BOOT in c for c in codes)


def patch_hotfixes(inst: Install) -> int:
    """Prepend the boot line to every cached hotfix that lacks it; originals go to the journal. Returns rows changed."""
    journal = json.loads(inst.journal.read_text()) if inst.journal.exists() else {"rows": []}
    saved = {(r["key"], r["scope"]): r for r in journal["rows"]}
    con = sqlite3.connect(inst.db)
    try:
        todo = [(k, s, c, decode(c)) for k, s, c in con.execute(HOTFIX_ROWS).fetchall()]
        todo = [(k, s, c, code) for k, s, c, code in todo if code is not None and BOOT not in code]
        for key, scope, content, _ in todo:
            saved[(key, scope)] = {"key": key, "scope": scope, "orig": content.hex()}
        inst.journal.write_text(json.dumps({"rows": list(saved.values())}))
        for key, scope, _, code in todo:
            con.execute("update localdb set content=? where key=? and scope=?", (encode(BOOT + "\n;" + code), key, scope))
        con.commit()
    finally:
        con.close()
    return len(todo)


def restore_hotfixes(inst: Install) -> tuple[int, int]:
    """Put journalled rows back where the boot line is still present. Returns (restored, left alone)."""
    if not inst.journal.exists():
        return 0, 0
    rows = json.loads(inst.journal.read_text())["rows"]
    restored = 0
    con = sqlite3.connect(inst.db)
    try:
        for r in rows:
            cur = con.execute("select content from localdb where key=? and scope=?", (r["key"], r["scope"])).fetchone()
            if cur and BOOT in (decode(cur[0]) or ""):
                con.execute("update localdb set content=? where key=? and scope=?",
                            (bytes.fromhex(r["orig"]), r["key"], r["scope"]))
                restored += 1
        con.commit()
    finally:
        con.close()
    inst.journal.unlink()
    return restored, len(rows) - restored


def cmd_status(inst: Install, _args) -> int:
    total, patched = hotfix_state(inst.db)
    loader = (inst.dir / LOADER).exists()
    if not loader and not patched:
        state = "not installed"
    elif loader and total and patched == total:
        state = "installed"
    else:
        state = "incomplete, run install"
    print(f"loader:   {state} (file {'present' if loader else 'missing'}, {patched} of {total} cached hotfixes patched)")
    enabled = inst.enabled()
    for name in inst.available():
        print(f"  {'on ' if name in enabled else 'off'} {name}")
    for name in enabled:
        if name not in inst.available():
            print(f"  on  {name}  (file missing)")
    return 0


def cmd_install(inst: Install, _args) -> int:
    if not inst.db.is_file():
        sys.exit(f"{inst.db} not found; start the game and log in once first")
    total, _ = hotfix_state(inst.db)
    if not total:
        sys.exit("no cached hotfixes yet; start the game and log in once first")
    inst.dir.mkdir(exist_ok=True)
    shutil.copyfile(LOADER_SOURCE, inst.dir / LOADER)
    changed = patch_hotfixes(inst)
    print(f"loader written, boot line added to {changed} cached hotfix(es) ({total} in total)")
    return 0


def cmd_uninstall(inst: Install, _args) -> int:
    restored, skipped = restore_hotfixes(inst) if inst.db.is_file() else (0, 0)
    (inst.dir / LOADER).unlink(missing_ok=True)
    print(f"restored {restored} cached hotfix(es)" + (f", {skipped} already replaced by the game" if skipped else "")
          + f"; loader removed, script files kept in {inst.dir}")
    return 0


def cmd_add(inst: Install, args) -> int:
    inst.dir.mkdir(exist_ok=True)
    enabled = inst.enabled()
    for item in args.names:
        src = Path(item)
        if not src.is_file():
            sys.exit(f"{item}: no such file")
        name = src.stem
        if name == Path(LOADER).stem:
            sys.exit("a script cannot be named 'loader'")
        shutil.copyfile(src, inst.dir / f"{name}.lua")
        if name not in enabled:
            enabled.append(name)
        print(f"added {name}")
    inst.set_enabled(enabled)
    return 0


def cmd_toggle(inst: Install, args) -> int:
    enabled = inst.enabled()
    for name in args.names:
        if args.cmd == "enable" and name not in inst.available():
            sys.exit(f"{name}: no such script in {inst.dir}")
        if args.cmd == "enable":
            if name not in enabled:
                enabled.append(name)
        else:
            enabled = [n for n in enabled if n != name]
            if args.cmd == "remove":
                (inst.dir / f"{name}.lua").unlink(missing_ok=True)
        print(f"{args.cmd}d {name}")
    inst.set_enabled(enabled)
    return 0


COMMANDS = {"status": cmd_status, "install": cmd_install, "uninstall": cmd_uninstall, "add": cmd_add,
            "enable": cmd_toggle, "disable": cmd_toggle, "remove": cmd_toggle}
WRITES_DB = {"install", "uninstall"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game_dir", type=Path, help="game install folder (the one holding LocalData)")
    ap.add_argument("cmd", choices=list(COMMANDS))
    ap.add_argument("names", nargs="*", help="script names, or files for add")
    ap.add_argument("--force", action="store_true", help="skip the game-running check")
    args = ap.parse_args()
    if args.cmd in ("add", "enable", "disable", "remove") and not args.names:
        ap.error(f"{args.cmd} needs at least one name")
    if args.cmd in WRITES_DB and not args.force and game_running():
        sys.exit("wwm.exe is running; close the game first (it rewrites LocalDB) or pass --force")
    return COMMANDS[args.cmd](Install(args.game_dir), args)


if __name__ == "__main__":
    sys.exit(main())
