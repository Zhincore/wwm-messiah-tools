#!/usr/bin/env python3
"""
Static game-data export: numeric id -> name (+ a few fields) per kind, from the HD data tables.

For mapping ids that appear in API/profile/chat payloads to display names (e.g. a Discord bot).
English text is resolved via the locale tables. Re-run after each game patch. Uses the plain tables,
not the `__grey` A/B variants (those are smaller alternate sets, not supersets).

Usage:
  wwm-gamedata <hd_dir> -o OUT [--locale en]      # e.g. <install>/LocalData/Patch/HD/oversea
Writes OUT/<kind>.json = { "<id>": { "id": <id>, "name": "...", ...extra } } and OUT/manifest.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .tables import Locale, Store

# kind -> (table, id field, name field, extra fields to carry through)
KINDS: list[tuple[str, str, str, str, list[str]]] = [
    ("gear", "equip_items", "id", "name", ["star", "equip_level", "equip_sub_type", "icon_no", "long_icon_no"]),
    ("martialArt", "kongfu", "kongfu_id", "name", ["kongfu_type", "weapon_type", "kongfu_icon", "school_id", "hide"]),
    ("innerWay", "xinfa", "id", "name", ["star", "type", "liupai_id", "icon_no"]),
    ("mysticSkill", "jianghu_skills", "jianghu_skill_no", "jianghu_skill_name", ["jianghu_icon", "function_type"]),
    ("avatar", "head", "avatar_head_id", "head_name", ["head_level", "head_male", "head_female"]),
    ("banner", "name_card_background_data", "card_id", "card_name", ["card_icon", "card_thumbnail"]),
    ("sect", "menpai_play_level_sect_config", "school_no", "name", ["icon"]),
]


def game_version(hd_dir: Path) -> str | None:
    """Built.version (e.g. '2026-11-02/r64785'), found by walking up to the install root."""
    for base in [hd_dir, *hd_dir.parents]:
        p = base / "Engine" / "Binaries" / "Win64r" / "Built.version"
        if p.exists():
            return p.read_text(errors="replace").strip()
    return None


def export_kind(store: Store, table: str, id_field: str, name_field: str, extra: list[str], keep_unnamed: bool):
    """{id: {id, name, ...extra}} for one table. id_field falls back to the row key; rows without a name skip."""
    if table not in store.manifest.tables:
        return None, 0, 0
    res = store.table(table)
    out, named = {}, 0
    for row in res["rows"]:
        rid = row.get(id_field, row.get("_key"))
        if rid is None:
            continue
        name = row.get(name_field)
        has_name = isinstance(name, str) and name != ""
        if has_name:
            named += 1
        elif not keep_unnamed:
            continue
        entry = {"id": rid, "name": name if has_name else None}
        for f in extra:
            v = row.get(f)
            if v is not None:
                entry[f] = v
        out[str(rid)] = entry
    return out, len(out), named


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hd_dir", help="HD store dir, e.g. <install>/LocalData/Patch/HD/oversea")
    ap.add_argument("-o", "--out", required=True, help="output directory for the JSON files")
    ap.add_argument("--locale", default="en", help="language for resolved names (default: en)")
    ap.add_argument("--all", action="store_true", help="include rows without a name (default: named only)")
    args = ap.parse_args()

    hd = Path(args.hd_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    store = Store(hd, Locale(hd, args.locale))

    manifest = {
        "game_version": game_version(hd),
        "generated": date.today().isoformat(),
        "locale": args.locale,
        "kinds": {},
    }
    for kind, table, id_field, name_field, extra in KINDS:
        data, count, named = export_kind(store, table, id_field, name_field, extra, args.all)
        if data is None:
            print(f"{kind}: table {table!r} not found, skipped", file=sys.stderr)
            manifest["kinds"][kind] = {"table": table, "missing": True}
            continue
        (out / f"{kind}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        manifest["kinds"][kind] = {"table": table, "id_field": id_field, "count": count, "named": named}
        print(f"{kind:12s} {table:32s} {count:5d} entries ({named} named)")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"version {manifest['game_version']} -> {out}/manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
