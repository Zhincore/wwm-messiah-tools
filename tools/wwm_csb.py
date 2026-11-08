#!/usr/bin/env python3
"""
Decode Cocos Studio .csb files (FlatBuffers, schema CSParseBinary from cocos2d-x v3).

The game's UI (UIScript/*.csb) uses the stock schema plus two extensions: ProjectNodeOptions
gains a 4th field, a vector of {classname, name, options} overriding children of the referenced
project file, and CSParseBinary gains two trailing fields (unresolved, empty so far). The table layout is read from cocos_csb_schema.json,
generated from cocos2d-x's CSParseBinary_generated.h.

Usage:
  wwm_csb.py FILE.csb [--json]        print a summary (tree, resources, texts, animations) or full JSON
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

SCHEMA = json.load(open(Path(__file__).resolve().parent / "cocos_csb_schema.json"))
SCALARS = {"uint8_t": ("<B", 1), "int8_t": ("<b", 1), "int16_t": ("<h", 2), "uint16_t": ("<H", 2),
           "int32_t": ("<i", 4), "uint32_t": ("<I", 4), "float": ("<f", 4), "int64_t": ("<q", 8),
           "double": ("<d", 8), "bool": ("<B", 1)}
STRUCT_FIELD_TYPES = {"float": "<f", "int32_t": "<i", "uint8_t": "<B", "int": "<i"}

# NodeTree.classname -> options table
CLASS_TABLE = {
    "Node": "SingleNodeOptions", "Layer": "SingleNodeOptions", "Sprite": "SpriteOptions",
    "Particle": "ParticleSystemOptions", "GameMap": "GameMapOptions", "ProjectNode": "ProjectNodeOptions",
    "ComAudio": "ComAudioOptions", "Widget": "WidgetOptions", "Layout": "PanelOptions",
    "Splendor": "SplendorOptions",  # NetEase shader-effect node: effect = "UI/UI<Effect>" material
}


class Reader:
    def __init__(self, buf: bytes):
        self.b = buf

    def u32(self, pos: int) -> int:
        if not 0 <= pos <= len(self.b) - 4:
            raise ValueError(f"offset {pos} outside buffer")
        return struct.unpack_from("<I", self.b, pos)[0]

    def i32(self, pos: int) -> int:
        if not 0 <= pos <= len(self.b) - 4:
            raise ValueError(f"offset {pos} outside buffer")
        return struct.unpack_from("<i", self.b, pos)[0]

    def string(self, pos: int) -> str:
        n = self.u32(pos)
        if pos + 4 + n > len(self.b):
            raise ValueError("string outside buffer")
        return self.b[pos + 4 : pos + 4 + n].decode("utf-8", errors="replace")

    def field_pos(self, table: int, voffset: int) -> int | None:
        vt = table - self.i32(table)
        vsize = struct.unpack_from("<H", self.b, vt)[0]
        if voffset + 2 > vsize:
            return None
        off = struct.unpack_from("<H", self.b, vt + voffset)[0]
        return table + off if off else None

    def struct_(self, pos: int, name: str) -> dict:
        out = {}
        for fname, ftype in SCHEMA["structs"][name]:
            fmt = STRUCT_FIELD_TYPES.get(ftype, "<f")
            out[fname] = struct.unpack_from(fmt, self.b, pos)[0]
            pos += struct.calcsize(fmt)
        return out

    def _widget_only(self, tpos: int) -> dict:
        """Options whose first field is the common WidgetOptions table, nothing else."""
        fp = self.field_pos(tpos, 4)
        if fp is None:
            return {}
        try:
            return {"widgetOptions": self.table(fp + self.u32(fp), "WidgetOptions")}
        except (ValueError, struct.error):
            return {}

    def table(self, pos: int, name: str, classname: str | None = None) -> dict:
        out: dict = {}
        for fname, voff, kind, targ, default in SCHEMA["tables"][name]:
            fp = self.field_pos(pos, voff)
            if kind == "scalar":
                if fp is None:
                    val = default
                    val = None if val is None else (float(val) if "." in val else int(val))
                else:
                    val = struct.unpack_from(SCALARS[targ][0], self.b, fp)[0]
                out[fname] = val
                continue
            if fp is None:
                continue
            if kind == "struct":
                out[fname] = self.struct_(fp, targ)
            elif kind == "string":
                out[fname] = self.string(fp + self.u32(fp))
            elif kind == "table":
                tpos = fp + self.u32(fp)
                if targ == "Table":  # Options.data: type comes from the parent node's classname
                    tname = CLASS_TABLE.get(classname or "", f"{classname}Options")
                    if tname not in SCHEMA["tables"]:
                        tname = "WidgetOptions"
                    try:
                        out[fname] = self.table(tpos, tname)
                        out["_options_type"] = tname
                    except (ValueError, struct.error, KeyError):
                        # unknown/extended class: keep only the common widget options
                        out[fname] = self._widget_only(tpos)
                        out["_options_type"] = f"unknown:{classname}"
                else:
                    out[fname] = self.table(tpos, targ)
            elif kind == "vector_table":
                vpos = fp + self.u32(fp)
                n = self.u32(vpos)
                items = []
                for i in range(n):
                    ep = vpos + 4 + 4 * i
                    tpos = ep + self.u32(ep)
                    if targ == "flatbuffers::String":
                        items.append(self.string(tpos))
                    else:
                        items.append(self.table(tpos, targ))
                out[fname] = items
            elif kind == "vector_scalar":
                vpos = fp + self.u32(fp)
                n = self.u32(vpos)
                if targ in SCHEMA["structs"]:  # vector of inline structs
                    size = sum(struct.calcsize(STRUCT_FIELD_TYPES.get(t, "<f")) for _, t in SCHEMA["structs"][targ])
                    out[fname] = [self.struct_(vpos + 4 + i * size, targ) for i in range(n)]
                else:
                    fmt, size = SCALARS[targ]
                    out[fname] = [struct.unpack_from(fmt, self.b, vpos + 4 + i * size)[0] for i in range(n)]
        if name in ("NodeTree", "ProjectNodeOverride") and "options" in out:
            # re-decode options with the right type now that classname is known
            fp = self.field_pos(pos, 8)
            if fp is not None:
                out["options"] = self.table(fp + self.u32(fp), "Options", out.get("classname"))
        return out


def decode(buf: bytes) -> dict:
    r = Reader(buf)
    return r.table(r.u32(0), "CSParseBinary")


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------


def walk(node: dict, depth: int = 0):
    """Depth-first over NodeTree, including the game's embedded ProjectNode sub-trees."""
    yield depth, node
    for c in node.get("children", []) or []:
        yield from walk(c, depth + 1)
    data = (node.get("options") or {}).get("data") or {}
    for c in data.get("overrides", []) or []:  # ProjectNode: per-child option overrides
        yield from walk(c, depth + 1)


def resources(csb: dict) -> list[dict]:
    """Every ResourceData reference (image, plist, font, project node) in the tree and timelines."""
    found = []

    def visit(obj, where):
        if isinstance(obj, dict):
            if "resourceType" in obj and ("path" in obj or "plistFile" in obj):
                found.append({"where": where, **{k: obj.get(k) for k in ("path", "plistFile", "resourceType")}})
            for k, v in obj.items():
                visit(v, where if k in ("widgetOptions", "nodeOptions", "data", "options") else k)
        elif isinstance(obj, list):
            for v in obj:
                visit(v, where)

    visit(csb.get("nodeTree"), "node")
    visit(csb.get("action"), "action")
    return found


def summary(csb: dict) -> dict:
    nodes, texts, anims = [], [], []
    for depth, n in walk(csb.get("nodeTree", {})):
        opt = (n.get("options") or {}).get("data") or {}
        w = opt.get("widgetOptions") or opt.get("nodeOptions") or opt
        nodes.append({"depth": depth, "class": n.get("classname"), "custom": n.get("customClassName"),
                      "name": w.get("name"), "size": w.get("size"), "pos": w.get("position"),
                      "file": (opt.get("fileName") or (opt.get("fileNameData") or {}).get("path")
                               or (opt.get("normalData") or {}).get("path"))})
        if opt.get("text"):
            texts.append({"node": w.get("name"), "text": opt["text"], "font": opt.get("fontName"),
                          "size": opt.get("fontSize"), "localized": opt.get("isLocalized")})
    for tl in (csb.get("action") or {}).get("timeLines", []) or []:
        frames = tl.get("frames", [])
        tex = [f["textureFrame"]["textureFile"].get("path") for f in frames if f.get("textureFrame", {}).get("textureFile")]
        anims.append({"property": tl.get("property"), "actionTag": tl.get("actionTag"), "frames": len(frames), "textures": tex})
    return {"version": csb.get("version"), "textures": csb.get("textures"), "texturePngs": csb.get("texturePngs"),
            "animations": [a.get("name") for a in csb.get("animationList", []) or []],
            "duration": (csb.get("action") or {}).get("duration"), "nodes": nodes, "texts": texts,
            "timelines": anims, "resources": resources(csb)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("--json", action="store_true", help="dump the full decoded tree")
    args = ap.parse_args()
    csb = decode(args.file.read_bytes())
    json.dump(csb if args.json else summary(csb), sys.stdout, ensure_ascii=False, indent=1)
    print()


if __name__ == "__main__":
    main()
