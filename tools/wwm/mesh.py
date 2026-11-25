#!/usr/bin/env python3
"""
.MESSIAH mesh decode -> Wavefront OBJ.

`.MESSIAH` is the Messiah engine's generic serialized-object wrapper. This module handles the
mesh-geometry sub-types (int `2`/`8`, FourCC `HRMD`, `SEMC`); skeleton/material/texture-meta
sub-types have no vertex descriptor and are rejected. Format and field layout: `docs/messiah-mesh.md`.

A container holds one or more mesh blocks (LODs / parts), each:
  u16 identifier, u16 subgrid_count, u32 vertex_count, u32 index_count
  4x descriptor string (u16 len + ASCII; vertex-stream layouts then `None` padding)
  bbox (6x f32: min xyz, max xyz)
  subgrid table (subgrid_count x 32B; last four u32 = start_index, index_count, start_vertex, vertex_count)
  index buffer (index_count x u16)
  vertex streams (planar, one block per layout, in order; stride from the layout codes)
  trailer bbox (52B)

A vertex-stream layout is `ATTR_ATTR_...`, each ATTR = `<Semantic><Count><Format>`:
  Semantic P=position N=normal T=texcoord/tangent W=blend-weight I=blend-index B=binormal C=color G=tangent
  Format   F=float32 H=float16 B=uint8 U=uint32 ; byte size = Count * sizeof(Format)

Usage:
  wwm-mesh inspect <file>              # report sub-type, blocks, counts, layouts, bbox
  wwm-mesh export  <file> -o out.obj   # positions + UV + normals + faces (all blocks)
<file> is a raw EZST block or an already-decoded payload (e.g. from `wwm-files export`).
"""

from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from .codecs import ezst_decode

MAGIC = b".MESSIAH"
_FMT = {"F": 4, "B": 1, "H": 2, "U": 4, "S": 2}
_SEM = {"P": "POSITION", "N": "NORMAL", "T": "TEXCOORD", "W": "BLENDWEIGHT",
        "I": "BLENDINDICES", "B": "BINORMAL", "C": "COLOR", "G": "TANGENT"}
_DESC_RE = re.compile(rb"[PNTWIBCG][0-9][FBHU]")
_TRAILER_BBOX = 52  # bytes of per-block trailer (end bounding data)


@dataclass(frozen=True, slots=True)
class Attr:
    """One vertex attribute within a stream layout token (e.g. `P3F`)."""
    token: str
    semantic: str
    count: int
    fmt: str

    @property
    def size(self) -> int:
        return self.count * _FMT[self.fmt]


@dataclass(frozen=True, slots=True)
class Stream:
    layout: str
    attrs: tuple[Attr, ...]

    @property
    def stride(self) -> int:
        return sum(a.size for a in self.attrs)


@dataclass(slots=True)
class Block:
    """One mesh block (LOD / part) within a `.MESSIAH` container."""
    identifier: int
    subgrids: list[tuple[int, int, int, int]]  # (start_index, index_count, start_vertex, vertex_count)
    vertex_count: int
    index_count: int
    streams: list[Stream]
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    indices: list[int]
    positions: list[tuple[float, float, float]]
    uvs: list[tuple[float, float]]
    normals: list[tuple[float, float, float]]
    end: int = 0  # file offset just past this block


def _parse_layout(s: str) -> Stream:
    attrs = []
    for tok in s.split("_"):
        if len(tok) >= 3 and tok[1].isdigit() and tok[2] in _FMT:
            attrs.append(Attr(tok, _SEM.get(tok[0], tok[0]), int(tok[1]), tok[2]))
    return Stream(s, tuple(attrs))


def _read_descriptors(data: bytes, pos: int) -> tuple[list[str], int]:
    out = []
    for _ in range(4):
        if pos + 2 > len(data):
            break
        n = struct.unpack_from("<H", data, pos)[0]
        if n == 0 or n > 64:
            break
        s = data[pos + 2 : pos + 2 + n]
        if not re.fullmatch(rb"[A-Za-z0-9_]+", s):
            break
        out.append(s.decode())
        pos += 2 + n
    return out, pos


def _decode_block(data: bytes, desc_start: int) -> Block:
    identifier, _subgrid = struct.unpack_from("<HH", data, desc_start - 12)
    vcount, icount = struct.unpack_from("<II", data, desc_start - 8)
    subgrid_count = _subgrid
    descriptors, pos = _read_descriptors(data, desc_start)
    streams = [_parse_layout(s) for s in descriptors if s != "None" and _DESC_RE.search(s.encode())]
    if not streams:
        raise ValueError("no vertex streams")
    # Cheap sanity gate: rejects non-mesh payloads whose bytes merely contain a descriptor-like
    # sequence (skeletons/materials), so the buffer search only runs on plausible meshes.
    if not (0 < vcount <= 2_000_000 and 3 <= icount <= 20_000_000 and icount % 3 == 0):
        raise ValueError("implausible vertex/index counts")

    bbox = struct.unpack_from("<6f", data, pos)
    if not all(b == b and abs(b) < 1e5 for b in bbox) or any(bbox[i] > bbox[i + 3] for i in range(3)):
        raise ValueError("implausible bounding box")
    pos += 24
    region = pos  # subgrid table + index/vertex buffers follow, in a layout that varies by mesh

    s0 = streams[0]
    poff = _attr_offset(s0, "POSITION")
    if poff is None:
        raise ValueError("stream has no position")
    vsize = vcount * sum(s.stride for s in streams)

    # The subgrid-table size and whether the index buffer precedes or follows the vertices are not
    # fixed across meshes, so the buffers are found by search: the vertex offset whose positions
    # reproduce the stored bbox AND that has a non-degenerate in-range index buffer on one side.
    vtx, idx_off, indices = _locate_buffers(data, region, vcount, icount, s0, poff, bbox, vsize)
    if vtx is None:
        raise ValueError("vertex/index buffers not found")

    uoff = _attr_offset(s0, "TEXCOORD", fmt="F")
    noff = _attr_offset(s0, "NORMAL", fmt="B")
    positions, uvs, normals = [], [], []
    for i in range(vcount):
        base = vtx + i * s0.stride
        positions.append(struct.unpack_from("<3f", data, base + poff))
        uvs.append(struct.unpack_from("<2f", data, base + uoff) if uoff is not None else (0.0, 0.0))
        if noff is not None:
            nb = data[base + noff : base + noff + 3]
            normals.append(tuple(b / 127.5 - 1.0 for b in nb))
        else:
            normals.append((0.0, 0.0, 0.0))

    subgrids = _read_subgrids(data, region, subgrid_count, vtx)
    idx_end = (idx_off + icount * 2) if idx_off is not None else 0
    end = max(vtx + vsize, idx_end) + _TRAILER_BBOX
    return Block(identifier, subgrids, vcount, icount, streams,
                 bbox[:3], bbox[3:], indices, positions, uvs, normals, end)


def _bbox_tol(bbox: tuple[float, ...]) -> float:
    """Tolerance scaled by the bbox diagonal (the stored bbox is slightly padded vs the vertices)."""
    diag = math.dist(bbox[:3], bbox[3:])
    return 2e-3 + 0.02 * diag


def _bbox_at(data: bytes, off: int, poff: int, stride: int, vcount: int,
             bbox: tuple[float, ...], tol: float) -> bool:
    """True when `vcount` positions at `off` fit inside `bbox` and fill most of it (early-exit)."""
    lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = bbox
    base = off + poff
    mnx = mny = mnz = float("inf")
    mxx = mxy = mxz = float("-inf")
    for i in range(vcount):
        x, y, z = struct.unpack_from("<3f", data, base + i * stride)
        if x != x or abs(x) > 1e5 or abs(y) > 1e5 or abs(z) > 1e5:
            return False
        mnx, mny, mnz = min(mnx, x), min(mny, y), min(mnz, z)
        mxx, mxy, mxz = max(mxx, x), max(mxy, y), max(mxz, z)
        if mnx < lo_x - tol or mny < lo_y - tol or mnz < lo_z - tol or \
           mxx > hi_x + tol or mxy > hi_y + tol or mxz > hi_z + tol:
            return False
    # Reject a tiny coincidental cluster: the decoded extent must cover most of the stored bbox.
    diag = math.dist(bbox[:3], bbox[3:])
    got = math.dist((mnx, mny, mnz), (mxx, mxy, mxz))
    return got >= 0.5 * diag - tol


def _index_side(data: bytes, off: int, vcount: int, icount: int):
    """If `icount` u16 at `off` are all < vcount, return (list, degenerate_tri_count); else None."""
    if off < 0 or off + icount * 2 > len(data):
        return None
    vals = struct.unpack_from("<%dH" % icount, data, off)
    if max(vals) >= vcount:
        return None
    degen = sum(1 for t in range(0, icount - 2, 3)
                if vals[t] == vals[t + 1] or vals[t + 1] == vals[t + 2] or vals[t] == vals[t + 2])
    return list(vals), degen


def _locate_buffers(data: bytes, start: int, vcount: int, icount: int, s0: Stream, poff: int,
                    bbox: tuple[float, ...], vsize: int):
    """Find (vtx_off, idx_off, indices): the vertex offset reproducing `bbox` with an in-range,
    mostly-non-degenerate index buffer on either side. Returns (None, None, []) if nothing fits."""
    stride = s0.stride
    tol = _bbox_tol(bbox)
    window = min(len(data) - vcount * stride, start + icount * 2 + 4096)
    max_degen = max(1, icount // 3 // 2)  # tolerate up to half-degenerate (strips, padding)
    off = start
    while off <= window:
        if _bbox_at(data, off, poff, stride, vcount, bbox, tol):
            best = None
            for cand in (off - icount * 2, off + vsize):
                side = _index_side(data, cand, vcount, icount)
                if side and (best is None or side[1] < best[2]):
                    best = (cand, side[0], side[1])
            if best and best[2] <= max_degen:
                return off, best[0], best[1]
            # bbox matched but no usable index buffer: keep scanning (symmetric false positive)
        off += 2
    return None, None, []


def _read_subgrids(data: bytes, region: int, count: int, vtx: int):
    """Best-effort submesh ranges (start_index, index_count, start_vertex, vertex_count), 32B each."""
    out = []
    if count and region + count * 32 <= vtx:
        for i in range(count):
            try:
                rec = struct.unpack_from("<8I", data, region + i * 32)
                out.append((rec[4], rec[5], rec[6], rec[7]))
            except struct.error:
                break
    return out


def _attr_offset(stream: Stream, semantic: str, fmt: str | None = None) -> int | None:
    off = 0
    for a in stream.attrs:
        if a.semantic == semantic and (fmt is None or a.fmt == fmt):
            return off
        off += a.size
    return None


def parse(data: bytes) -> list[Block]:
    """Decode every mesh block in a `.MESSIAH` payload. Raises if it is not a mesh container."""
    if data[:8] != MAGIC:
        raise ValueError("not a .MESSIAH payload")
    first = _DESC_RE.search(data)
    if not first:
        raise ValueError("no vertex descriptor (not a mesh sub-type)")
    blocks = []
    pos = first.start() - 2
    while True:
        try:
            block = _decode_block(data, pos)
        except (ValueError, struct.error):
            break
        # The first block anchors the container; later candidates (found by scanning the
        # trailer) are accepted only when they decode and reproduce their own bbox, which
        # stops the walk at the first spurious descriptor match.
        if blocks and not bbox_matches(block):
            break
        blocks.append(block)
        nxt = _DESC_RE.search(data, block.end - 64)
        if not nxt:
            break
        start = nxt.start() - 2
        if start <= pos:
            break
        pos = start
    if not blocks:
        raise ValueError("no decodable mesh block")
    return blocks


def bbox_matches(block: Block) -> bool:
    """Decode self-check: positions sit inside the (slightly padded) stored bbox and fill most of it."""
    if not block.positions:
        return False
    bbox = (*block.bbox_min, *block.bbox_max)
    tol = _bbox_tol(bbox)
    xs = [p[0] for p in block.positions]
    ys = [p[1] for p in block.positions]
    zs = [p[2] for p in block.positions]
    lo, hi = (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
    if any(lo[i] < block.bbox_min[i] - tol or hi[i] > block.bbox_max[i] + tol for i in range(3)):
        return False
    return math.dist(lo, hi) >= 0.5 * math.dist(block.bbox_min, block.bbox_max) - tol


def to_obj(blocks: list[Block], name: str) -> str:
    lines, base = [f"# {name}", "# decoded by wwm-mesh"], 0
    for bi, b in enumerate(blocks):
        lines.append(f"o block{bi}")
        for x, y, z in b.positions:
            lines.append("v %.6f %.6f %.6f" % (x, y, z))
        for u, v in b.uvs:
            lines.append("vt %.6f %.6f" % (u, 1.0 - v))
        for nx, ny, nz in b.normals:
            lines.append("vn %.4f %.4f %.4f" % (nx, ny, nz))
        idx = b.indices
        for t in range(0, len(idx) - 2, 3):
            a, c, d = idx[t] + 1 + base, idx[t + 1] + 1 + base, idx[t + 2] + 1 + base
            lines.append(f"f {a}/{a}/{a} {c}/{c}/{c} {d}/{d}/{d}")
        base += len(b.positions)
    return "\n".join(lines) + "\n"


def _load_payload(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] in (b"EZST", b"ZSTD"):
        data = ezst_decode(data)
    return data


def cmd_inspect(args: argparse.Namespace) -> int:
    data = _load_payload(Path(args.file))
    if data[:8] != MAGIC:
        print("not a .MESSIAH payload", file=sys.stderr)
        return 1
    subtype = data[8:12]
    label = subtype.decode("latin1") if subtype.isalpha() else "int%d" % struct.unpack_from("<I", data, 8)[0]
    try:
        blocks = parse(data)
    except ValueError as e:
        print(f"{label}: {e}")
        return 1
    print(f"sub-type {label}; {len(blocks)} mesh block(s)")
    for i, b in enumerate(blocks):
        layouts = ", ".join(f"{s.layout}({s.stride}B)" for s in b.streams)
        print(f"  block {i}: {b.vertex_count} verts, {b.index_count // 3} tris, "
              f"{len(b.subgrids)} subgrid(s); streams [{layouts}]")
        print(f"    bbox min {tuple(round(x, 3) for x in b.bbox_min)} "
              f"max {tuple(round(x, 3) for x in b.bbox_max)}; self-check {'ok' if bbox_matches(b) else 'MISMATCH'}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    data = _load_payload(Path(args.file))
    if data[:8] != MAGIC:
        print("not a .MESSIAH payload", file=sys.stderr)
        return 1
    try:
        blocks = parse(data)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if not any(bbox_matches(b) for b in blocks) and not args.force:
        print("decode self-check failed (bbox mismatch); use --force to write anyway", file=sys.stderr)
        return 1
    out = Path(args.output)
    out.write_text(to_obj(blocks, Path(args.file).name))
    v = sum(len(b.positions) for b in blocks)
    t = sum(b.index_count // 3 for b in blocks)
    print(f"wrote {out}: {len(blocks)} block(s), {v} verts, {t} tris")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect", help="report container sub-type, blocks, counts, layouts, bbox")
    i.add_argument("file")
    i.set_defaults(func=cmd_inspect)
    e = sub.add_parser("export", help="decode to a Wavefront OBJ")
    e.add_argument("file")
    e.add_argument("-o", "--output", required=True)
    e.add_argument("--force", action="store_true", help="write even if the bbox self-check fails")
    e.set_defaults(func=cmd_export)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
