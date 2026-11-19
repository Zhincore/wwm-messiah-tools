#!/usr/bin/env python3
"""
Bulk export of HD data tables by topic: every table of a dataset as one JSON file, text ids resolved.

A dataset is a list of table-name patterns (DATASETS). Rows are written as the tables hold them, with the
game's field names; docs/datasets.md says what is known about each. Uses the plain tables, not the
`__grey` A/B variants. Re-run after each game patch.

Usage:
  wwm-data raw <hd_dir> --list                       # datasets with table and row counts
  wwm-data raw <hd_dir> -o OUT [dataset ...]         # default: all datasets
  wwm-data raw <hd_dir> -o OUT --tables REGEX        # any tables, written to OUT/custom/
Writes OUT/<dataset>/<table>.json = { "<row key>": row } and OUT/manifest.json. Tables whose keys repeat
(marked "multi" in the manifest) map each key to the list of its rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from .gamedata import game_version
from .tables import Locale, Store

GREY = "__grey"

# dataset -> (what it holds, table-name patterns)
DATASETS: dict[str, tuple[str, list[str]]] = {
    "gacha": ("draw pools, their rewards, odds and pity", [r"^gacha_", r"^activity_gacha_", r"^reward_gacha_", r"lottery"]),
    "rewards": ("reward and drop tables", [r"^rewards?(_|$)"]),
    "gear": ("gear items, stat pools, enhancement, sets", [r"^equip_", r"^suit_", r"^tone_", r"^toning_", r"^average_enhance"]),
    "inner_ways": ("inner ways, their ranks and passives, paths", [r"^xinfa", r"_xinfa", r"^neigong_"]),
    "martial_arts": ("martial arts and mystic skills", [r"^kongfu", r"^jianghu_skill", r"^qishu"]),
    "attributes": ("attribute definitions and formula parameters", [r"^formula_", r"attr_to_cn_name$", r"^combat_const_data$", r"^entity_attr$", r"^calcpoint$"]),
    "guild": ("guild config incl. Guild War, events, skills, buildings", [r"^club_"]),
    "arena": ("arena grades, seasons, rankings", [r"^lunjian", r"^season_reward", r"^rank_", r"^identity_pvp"]),
    "items": ("item catalogue, sources, crafting recipes", [r"^stuff_", r"^compound_"]),
}


def select(names, patterns: list[str]) -> list[str]:
    """Plain (non-grey) table names matching any pattern, sorted."""
    compiled = [re.compile(p) for p in patterns]
    return sorted(n for n in names if not n.endswith(GREY) and any(c.search(n) for c in compiled))


def table_rows(store: Store, name: str) -> tuple[dict[str, object], int, bool]:
    """
    {row key: row} for one table (a bare value stays bare), the number of undecodable rows, and whether
    keys repeat. In a table with repeated keys every key maps to the list of its rows instead.
    """
    res = store.table(name)
    grouped: dict[str, list] = {}
    for row in res["rows"]:
        rest = {k: v for k, v in row.items() if k != "_key"}
        grouped.setdefault(str(row["_key"]), []).append(rest["value"] if set(rest) == {"value"} else rest)
    multi = any(len(rows) > 1 for rows in grouped.values())
    return (grouped if multi else {k: rows[0] for k, rows in grouped.items()}), res.get("errors", 0), multi


def export(store: Store, out: Path, tables: list[str]) -> dict[str, dict[str, int]]:
    """Write each table to out/<table>.json; returns {table: {rows, errors[, multi]}}."""
    out.mkdir(parents=True, exist_ok=True)
    done = {}
    for name in tables:
        rows, errors, multi = table_rows(store, name)
        (out / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        count = sum(len(v) for v in rows.values()) if multi else len(rows)
        done[name] = {"rows": count, "errors": errors, **({"multi": True} if multi else {})}
    return done


def add_parser(sub) -> None:
    ap = sub.add_parser("raw", help="whole tables by topic", description=__doc__,
                        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hd_dir", help="HD store dir, e.g. <install>/LocalData/Patch/HD/oversea")
    ap.add_argument("datasets", nargs="*", help=f"datasets to export (default all): {', '.join(DATASETS)}")
    ap.add_argument("-o", "--out", help="output directory")
    ap.add_argument("--list", action="store_true", help="list datasets with table and row counts")
    ap.add_argument("--tables", metavar="REGEX", help="export every table matching REGEX to OUT/custom/")
    ap.add_argument("--locale", default="en", help="language for resolved text (default: en)")
    ap.set_defaults(run=run, parser=ap)


def run(args) -> int:
    ap = args.parser
    unknown = [d for d in args.datasets if d not in DATASETS]
    if unknown:
        ap.error(f"unknown dataset(s): {', '.join(unknown)}")
    hd = Path(args.hd_dir)
    store = Store(hd, None if args.list else Locale(hd, args.locale))
    known = store.manifest.tables

    if args.list:
        for name, (what, patterns) in DATASETS.items():
            tables = select(known, patterns)
            print(f"{name:13s} {len(tables):4d} tables {sum(known[t].rows for t in tables):7d} rows  {what}")
        return 0
    if not args.out:
        ap.error("-o/--out is required")

    wanted = {"custom": [args.tables]} if args.tables else {
        name: DATASETS[name][1] for name in (args.datasets or DATASETS)
    }
    out = Path(args.out)
    manifest = {
        "game_version": game_version(hd),
        "generated": date.today().isoformat(),
        "locale": args.locale,
        "datasets": {},
    }
    for name, patterns in wanted.items():
        tables = select(known, patterns)
        done = export(store, out / name, tables)
        errors = sum(t["errors"] for t in done.values())
        manifest["datasets"][name] = done
        print(f"{name:13s} {len(done):4d} tables {sum(t['rows'] for t in done.values()):7d} rows"
              + (f"  {errors} undecodable rows" if errors else ""), file=sys.stderr if errors else sys.stdout)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"version {manifest['game_version']} -> {out}/manifest.json")
    return 0

