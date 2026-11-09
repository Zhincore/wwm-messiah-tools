#!/usr/bin/env python3
"""
Build a UI asset map for Where Winds Meet from Cocos .csb widgets, .plist atlases and
harvested asset paths.

Inputs
  --csb-dir   directory of <file_id>.csb payloads (from wwm_harvest_paths.py --save-csb)
  --paths     harvested path list (wwm_harvest_paths.py -o)
  --manifest  texture manifest from wwm_textures.py (to link png names to extracted files)
  install_root  to pull .plist atlases out of the packs by id

Outputs (in -o DIR)
  names.csv      file_id -> recovered path (every harvested path that resolves to an index entry)
  csb/*.json     one decoded summary per widget file, named by recovered path
  ui_nodes.csv   csb, node path, class, name, size, image, plist, text
  atlases.csv    plist path -> texture png -> frame name, rect, rotated
  textures.csv   extracted png file -> recovered path (when known)
  timelines.csv  csb, timeline property, frame count, texture sequence (frame animations)
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import plistlib
import re
import sys
from collections import defaultdict
from pathlib import Path

_here = Path(__file__).resolve().parent


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _here / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


wt = _load("wwm_textures")
hp = _load("wwm_harvest_paths")
csbmod = _load("wwm_csb")


def safe_name(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", path)[:150]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root")
    ap.add_argument("--csb-dir", required=True)
    ap.add_argument("--paths", required=True)
    ap.add_argument("--manifest")
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()
    out = Path(args.output)
    (out / "csb").mkdir(parents=True, exist_ok=True)

    # ---- names: harvested path -> file_id -> index entry
    root = Path(args.install_root)
    entries = wt.collect_entries(root, {"res", "lt", "cache", "db"})
    by_id: dict[int, wt.Entry] = {}
    for e in entries:
        by_id.setdefault(e.file_id, e)
    names: dict[int, str] = {}
    for line in open(args.paths, "rb"):
        p = line.strip().decode("utf-8", errors="ignore")
        if not p:
            continue
        fid = hp.file_id(p)
        if fid in by_id:
            names.setdefault(fid, p)
    with open(out / "names.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file_id", "path", "container", "offset", "size"])
        for fid, p in sorted(names.items(), key=lambda x: x[1]):
            e = by_id[fid]
            w.writerow([f"{fid:016x}", p, Path(e.container).name, e.offset, e.size])
    print(f"names: {len(names)} of {sum(1 for _ in open(args.paths, 'rb'))} harvested paths resolve to index entries", flush=True)

    # ---- csb widgets
    node_rows, tl_rows = [], []
    csb_files = sorted(Path(args.csb_dir).glob("*.csb"))
    for cf in csb_files:
        fid = int(cf.stem, 16)
        name = names.get(fid, cf.stem)
        try:
            s = csbmod.summary(csbmod.decode(cf.read_bytes()))
        except Exception as e:
            print(f"  csb {name}: decode failed: {e}", file=sys.stderr)
            continue
        json.dump(s, open(out / "csb" / (safe_name(name) + ".json"), "w"), ensure_ascii=False, indent=1)
        stack: list[str] = []
        for n in s["nodes"]:
            stack = stack[: n["depth"]] + [n["name"] or n["class"] or "?"]
            node_rows.append([name, "/".join(stack), n["class"], n["name"], n["size"] and f'{n["size"]["width"]:g}x{n["size"]["height"]:g}', n["file"] or ""])
        for t in s["texts"]:
            node_rows.append([name, t["node"] or "", "Text", t["node"], "", "", t["text"], t["font"], t["size"]])
        for tl in s["timelines"]:
            if tl["textures"]:
                tl_rows.append([name, tl["property"], tl["actionTag"], tl["frames"], "|".join(x or "" for x in tl["textures"])])
    with open(out / "ui_nodes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["csb", "node_path", "class", "name", "size", "image_or_file", "text", "font", "font_size"])
        w.writerows(node_rows)
    with open(out / "timelines.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["csb", "property", "action_tag", "frames", "textures"])
        w.writerows(tl_rows)
    print(f"csb: {len(csb_files)} files, {len(node_rows)} node rows, {len(tl_rows)} texture timelines", flush=True)

    # ---- plist atlases pulled from the packs by id
    plist_ids = [(fid, p) for fid, p in names.items() if p.lower().endswith(".plist")]
    packs: dict[str, wt.Pack] = {}
    frame_rows = []
    atlas_textures: dict[str, str] = {}
    for fid, p in plist_ids:
        e = by_id[fid]
        pack = packs.setdefault(e.container, wt.Pack(e.container))
        if pack.blocks is not None:
            continue
        buf = pack.read(e.offset, e.size)
        if buf[:4] in (b"EZST", b"ZSTD"):
            try:
                buf = wt.ezst_decode(buf)
            except Exception:
                continue
        try:
            pl = plistlib.loads(buf)
        except Exception:
            continue
        tex = (pl.get("metadata") or {}).get("textureFileName") or (pl.get("metadata") or {}).get("realTextureFileName") or ""
        atlas_textures[p] = tex
        for fname, fr in (pl.get("frames") or {}).items():
            rect = fr.get("frame") or fr.get("textureRect") or ""
            frame_rows.append([p, tex, fname, rect, fr.get("rotated") or fr.get("textureRotated") or False, fr.get("sourceSize") or fr.get("spriteSourceSize") or ""])
    with open(out / "atlases.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["plist", "texture", "frame", "rect", "rotated", "source_size"])
        w.writerows(frame_rows)
    print(f"atlases: {len(atlas_textures)} plists, {len(frame_rows)} frames", flush=True)

    # ---- textures: extracted png -> recovered path
    if args.manifest:
        n = 0
        with open(out / "textures.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["file_id", "path", "status", "format", "width", "height", "file"])
            for r in csv.DictReader(open(args.manifest, newline="")):
                p = names.get(int(r["file_id"], 16))
                if p:
                    n += 1
                    w.writerow([r["file_id"], p, r["status"], r["format"], r["width"], r["height"], r["path"]])
        print(f"textures: {n} extracted textures named", flush=True)


if __name__ == "__main__":
    main()
