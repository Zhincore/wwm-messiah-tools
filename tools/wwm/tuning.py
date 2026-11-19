#!/usr/bin/env python3
"""
Static export of the gear roll tables: which stats an item can roll, with what weight and range.

Covers the first stat of a dropped item, tuning, attunement and the set/base-attribute rolls, for every
gear tier. Source field names are kept; docs/gear-tuning.md explains them and the roll math.
Uses the plain tables, not the `__grey` A/B variants. Re-run after each game patch.

Usage:
  wwm-tuning <hd_dir> -o OUT [--locale en]      # e.g. <install>/LocalData/Patch/HD/oversea
Writes OUT/{affixes,pools,attunements,items,levels,materials,legacy,config,paths,sets}.json + manifest.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .gamedata import game_version
from .tables import Locale, Store

# equip_items fields that drive rolls, copied as they are
ITEM_FIELDS = [
    "name", "star", "equip_level", "type", "subtype", "equip_sub_type",
    "initia_group", "re_initial_num", "re_group", "re_cost", "re_reset_cost", "more_re_tone_chance",
    "chosen_det_count", "det_swap_group", "same_level_legacy", "suffix",
]
DET_PREFIX = "det_group_"


def rows_by_key(store: Store, table: str) -> dict[int, dict] | None:
    """{row key: row without `_key`} for one table, or None when the table is absent."""
    if table not in store.manifest.tables:
        return None
    return {r["_key"]: {k: v for k, v in r.items() if k != "_key"} for r in store.table(table)["rows"]}


def affixes(config: dict[int, dict], attrs: dict[int, dict]) -> dict[int, dict]:
    """equip_affix_config rows plus `name` (the description, else the attribute's name) and `attr` (its key)."""
    out = {}
    for no, row in config.items():
        attr = attrs.get(row.get("affix_para"), {})
        des = row.get("affix_des")
        name = des if isinstance(des, str) and des else attr.get("prop_name_cn")
        out[no] = {"name": name if isinstance(name, str) else None, "attr": attr.get("prop_name_en"), **row}
    return out


def items(equips: dict[int, dict], base_attrs: list[str]) -> dict[int, dict]:
    """Roll-related fields of every gear item that rolls stats: pools, attunement types, base attributes, sets."""
    out = {}
    for no, row in equips.items():
        if not any(row.get(f) for f in ("initia_group", "re_group", "suffix")):
            continue
        item = {f: row[f] for f in ITEM_FIELDS if row.get(f) is not None}
        det = {int(k[len(DET_PREFIX):]): v for k, v in row.items() if k.startswith(DET_PREFIX) and v is not None}
        if det:
            item["det_groups"] = dict(sorted(det.items()))
        base = {a: row[a] for a in base_attrs if row.get(a) is not None}
        if base:
            item["base_attrs"] = base
        out[no] = item
    return out


def material_names(stuff: dict[int, dict], *sources) -> dict[int, str]:
    """Names of every item id that appears anywhere in the given structures (costs, materials)."""
    found: dict[int, str] = {}

    def walk(v):
        if isinstance(v, bool):
            return
        if isinstance(v, int):
            name = stuff.get(v, {}).get("name")
            if isinstance(name, str) and name:
                found[v] = name
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(k)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    for s in sources:
        walk(s)
    return dict(sorted(found.items()))


def build(store: Store) -> tuple[dict[str, object], list[str]]:
    """All output files as {name: data}, and the names of source tables that were missing."""
    missing: list[str] = []

    def table(name: str) -> dict[int, dict]:
        rows = rows_by_key(store, name)
        if rows is None:
            missing.append(name)
            return {}
        return rows

    attrs = table("formula_base_attrs")
    cols = table("equip_attr_cols").get(1, {})
    props = table("toning_prop_config")
    floors = table("tone_critical_props")
    exp = table("equip_toning_props")
    attunements = table("equip_determin_group")
    equips = items(table("equip_items"), [a for a in cols.get("base", []) if isinstance(a, str)])
    materials = {"exp": exp, "floor": floors, "per_level": props}
    materials["names"] = material_names(table("stuff_items"), list(exp), list(floors), props, attunements,
                                        [i.get("re_reset_cost") for i in equips.values()])
    out = {
        "affixes": affixes(table("equip_affix_config"), attrs),
        "pools": table("equip_toning_group"),
        "attunements": attunements,
        "items": equips,
        "levels": table("equip_toning_mode"),
        "materials": materials,
        "legacy": table("equip_legacy"),
        "config": table("equip_config").get(1, {}),
        "paths": {k: v.get("name") for k, v in table("neigong_liupai_data").items()},
        "sets": {k: {"name": v.get("name"), "suit_n": v.get("suit_n")} for k, v in table("suit_affix_data").items()},
    }
    return out, missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hd_dir", help="HD store dir, e.g. <install>/LocalData/Patch/HD/oversea")
    ap.add_argument("-o", "--out", required=True, help="output directory for the JSON files")
    ap.add_argument("--locale", default="en", help="language for resolved names (default: en)")
    args = ap.parse_args()

    hd = Path(args.hd_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    data, missing = build(Store(hd, Locale(hd, args.locale)))
    for name in missing:
        print(f"table {name!r} not found, skipped", file=sys.stderr)

    manifest = {
        "game_version": game_version(hd),
        "generated": date.today().isoformat(),
        "locale": args.locale,
        "missing_tables": missing,
        "files": {},
    }
    for name, content in data.items():
        (out / f"{name}.json").write_text(json.dumps(content, ensure_ascii=False, indent=1), encoding="utf-8")
        manifest["files"][name] = len(content)
        print(f"{name:12s} {len(content):6d} entries")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"version {manifest['game_version']} -> {out}/manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
