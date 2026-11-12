#!/usr/bin/env python3
"""
Cut sprite frames out of a Cocos .plist atlas stored in the packs, and optionally build
animated GIFs from numbered frame sequences (e.g. ui_emotion_12_00000 .. 00004).

Usage:
  wwm-atlas <install_root> --plist UIScript/plist/foo.plist -o OUT [--gif] [--fps 10]
  wwm-atlas <install_root> --list PATTERN --paths paths.txt   # find plists by name

The plist's textureFileName is resolved by hashing candidate paths next to the plist.
The texture is a game texture container (see wwm.textures); frames are cut using the
plist rects (rotated frames are un-rotated) and written as <frame>.png.
"""

from __future__ import annotations

import argparse
import io
import plistlib
import re
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

from . import codecs, ids, textures, vfs


SEQ_RE = re.compile(r"^(.*?)[_-]?(\d{2,6})\.png$")


def parse_rect(s: str) -> tuple[int, int, int, int]:
    x, y, w, h = (int(float(v)) for v in re.findall(r"-?[\d.]+", s))
    return x, y, w, h


def read_entry(by_id: dict, path: str) -> tuple[bytes, str] | None:
    e = by_id.get(ids.file_id(path))
    if e is None:
        return None
    pack = vfs.Pack(e.container)
    buf = pack.read(e.offset, e.size)
    if buf[:4] in (b"EZST", b"ZSTD"):
        buf = codecs.ezst_decode(buf)
    return buf, path


def texture_image(buf: bytes) -> Image.Image:
    if buf[:4] == vfs.TEX_MAGIC:
        tex = textures.parse_texture(buf)
        top = max((m for m in tex.mips if m.data), key=lambda m: m.w * m.h)
        png = textures.to_png(tex.fmt, top.w, top.h, top.data)
        if png is None:
            raise ValueError(f"cannot decode texture format {tex.fmt}")
        return Image.open(io.BytesIO(png)).convert("RGBA")
    return Image.open(io.BytesIO(buf)).convert("RGBA")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("install_root")
    ap.add_argument("--plist", help="plist path inside the VFS, e.g. UIScript/plist/x.plist")
    ap.add_argument("--list", metavar="REGEX", help="list harvested plist paths matching REGEX and exit")
    ap.add_argument("--paths", help="harvested paths file (for --list)")
    ap.add_argument("-o", "--output")
    ap.add_argument("--texture-id", help="file id (hex) of the atlas texture when it cannot be resolved by name")
    ap.add_argument("--gif", action="store_true", help="also build one GIF per numbered frame sequence (1-bit alpha)")
    ap.add_argument("--webp", action="store_true", help="also build lossless animated WebP per sequence (full alpha)")
    ap.add_argument("--fps", type=float, default=10.0)
    args = ap.parse_args()

    if args.list:
        rx = re.compile(args.list, re.I)
        for line in open(args.paths):
            p = line.strip()
            if p.lower().endswith(".plist") and rx.search(p):
                print(p)
        return
    if not (args.plist and args.output):
        ap.error("--plist and -o are required")

    root = Path(args.install_root)
    entries = vfs.collect_entries(root, {"res", "lt", "cache", "db"})
    by_id = {}
    for e in entries:
        by_id.setdefault(e.file_id, e)

    got = read_entry(by_id, args.plist)
    if got is None:
        sys.exit(f"plist not found in indexes: {args.plist}")
    pl = plistlib.loads(got[0])
    meta = pl.get("metadata") or {}
    tex_name = meta.get("textureFileName") or meta.get("realTextureFileName")
    frames = pl.get("frames") or {}
    print(f"{args.plist}: {len(frames)} frames, texture {tex_name}, format {meta.get('format')}")

    plist_dir = args.plist.rsplit("/", 1)[0] + "/" if "/" in args.plist else ""
    candidates = [plist_dir + tex_name, tex_name, "UIScript/" + tex_name, "UI/" + tex_name,
                  plist_dir.replace("plist/", "") + tex_name]
    tex = next((r for r in (read_entry(by_id, c) for c in candidates) if r), None)
    if tex is None and args.texture_id:
        e = by_id.get(int(args.texture_id, 16))
        if e:
            pack = vfs.Pack(e.container)
            buf = pack.read(e.offset, e.size)
            tex = (codecs.ezst_decode(buf) if buf[:4] in (b"EZST", b"ZSTD") else buf, f"id:{args.texture_id}")
    if tex is None:
        sys.exit(f"texture not found, tried: {candidates}; pass --texture-id (textures are GUID-keyed)")
    print(f"texture resolved as {tex[1]}")
    atlas = texture_image(tex[0])
    print(f"atlas image {atlas.size}")

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    atlas.save(out / Path(tex_name).name)
    seqs: dict[str, list[tuple[int, Image.Image]]] = defaultdict(list)
    for name, fr in frames.items():
        x, y, w, h = parse_rect(fr.get("frame") or fr.get("textureRect"))
        rotated = bool(fr.get("rotated") or fr.get("textureRotated"))
        box = (x, y, x + h, y + w) if rotated else (x, y, x + w, y + h)
        im = atlas.crop(box)
        if rotated:
            im = im.rotate(90, expand=True)
        # place the trimmed frame back into its original source size
        src = fr.get("sourceSize") or fr.get("spriteSourceSize")
        off = fr.get("offset") or fr.get("spriteOffset")
        if src and off:
            sw, sh = (int(float(v)) for v in re.findall(r"-?[\d.]+", src))
            ox, oy = (float(v) for v in re.findall(r"-?[\d.]+", off))
            canvas = Image.new("RGBA", (sw, sh))
            px = int(round((sw - w) / 2 + ox))
            py = int(round((sh - h) / 2 - oy))
            canvas.paste(im, (px, py))
            im = canvas
        im.save(out / name)
        m = SEQ_RE.match(name)
        if m:
            seqs[m.group(1)].append((int(m.group(2)), im))
    print(f"wrote {len(frames)} frames to {out}")

    duration = int(1000 / args.fps)
    if args.webp:
        webp_dir = out / "webp"
        webp_dir.mkdir(exist_ok=True)
        n = 0
        for base, items in seqs.items():
            items.sort()
            ims = [im for _, im in items]
            if len(ims) < 2:
                continue
            ims[0].save(webp_dir / f"{base}.webp", save_all=True, append_images=ims[1:],
                        duration=duration, loop=0, lossless=True, exact=True)
            n += 1
        print(f"wrote {n} animated WebPs (lossless, full alpha) to {webp_dir}")
    if args.gif:
        gif_dir = out / "gif"
        gif_dir.mkdir(exist_ok=True)
        n = 0
        for base, items in seqs.items():
            items.sort()
            ims = [gif_frame(im) for _, im in items]
            if len(ims) < 2:
                continue
            ims[0].save(gif_dir / f"{base}.gif", save_all=True, append_images=ims[1:],
                        duration=duration, loop=0, disposal=2, transparency=255)
            n += 1
        print(f"wrote {n} GIFs to {gif_dir}")


def gif_frame(im: Image.Image) -> Image.Image:
    """
    RGBA -> palette frame with a clean 1-bit alpha: pixels below the alpha threshold become
    palette index 255 (declared transparent), everything else is quantised from premultiplied
    colour so semi-transparent edges do not keep stray colours.
    """
    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    opaque_mask = alpha.point(lambda a: 255 if a >= 96 else 0)
    # flatten semi-transparent edge pixels against black before quantising
    flat = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    flat.paste(rgba, mask=alpha)
    pal = flat.convert("RGB").quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    pal.paste(255, mask=opaque_mask.point(lambda v: 255 - v))
    return pal


if __name__ == "__main__":
    main()
