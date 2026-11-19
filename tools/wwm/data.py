#!/usr/bin/env python3
"""
Game-data exports from the HD data tables (read-only). Re-run after each game patch.

  wwm-data raw    <hd_dir> -o OUT [dataset ...]    whole tables by topic            (datasets.py, docs/datasets.md)
  wwm-data names  <hd_dir> -o OUT                  id -> name maps per kind         (gamedata.py)
  wwm-data tuning <hd_dir> -o OUT                  gear roll tables, shaped         (tuning.py, docs/gear-tuning.md)
  wwm-data formulas <hd_dir> -o OUT                attribute formulas, decoded      (formula.py, docs/formula-ft.md)
  wwm-data flows <lua_dump> -o OUT                 damage and heal flows, plain Lua (formula.py, docs/damage-flows.md)

<hd_dir> is the HD store, e.g. <install>/LocalData/Patch/HD/oversea. `wwm-data <command> -h` for options.
"""

from __future__ import annotations

import argparse

from . import datasets, formula, gamedata, tuning


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True, metavar="{raw,names,tuning,formulas,flows}")
    for module in (datasets, gamedata, tuning, formula):
        module.add_parser(sub)
    args = ap.parse_args()
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
