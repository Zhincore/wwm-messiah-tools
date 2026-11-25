#!/usr/bin/env python3
"""
.MESSIAH mesh decode -> Wavefront OBJ.

`.MESSIAH` is the Messiah engine's generic serialized-object wrapper. This module handles the
mesh-geometry sub-types (int `8`, FourCC `HRMD`, `SEMC`); other sub-types have no vertex descriptor
and are rejected. Format and field layout: `docs/messiah-mesh.md`.

A container holds one or more mesh blocks (LODs / parts), each:
  u16 identifier, u16 subgrid_count, u32 vertex_count, u32 index_count
  4x descriptor string (u16 len + ASCII; vertex-stream layouts then `None` padding)
  bbox (6x f32: min xyz, max xyz; slightly padded)
  subgrid table, index buffer (index_count x u16) and planar vertex streams, in a mesh-dependent
  order: the buffers are located by search (positions reproduce the bbox, indices in range)
  trailer (52B)

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
_FMT = {"F": 4, "B": 1, "H": 2, "U": 4}
_SEM = {"P": "POSITION", "N": "NORMAL", "T": "TEXCOORD", "W": "BLENDWEIGHT",
        "I": "BLENDINDICES", "B": "BINORMAL", "C": "COLOR", "G": "TANGENT"}
_DESC_RE = re.compile(rb"[PNTWIBCG][0-9][FBHU]")
_NAME_RE = re.compile(rb"[A-Za-z0-9_]+")
_TRAILER_SIZE = 52
_SUBGRID_SIZE = 32
_MAX_COORD = 1e5

Bbox = tuple[float, float, float, float, float, float]


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

    def offset(self, semantic: str, fmt: str | None = None) -> int | None:
        """Byte offset of the first attribute with `semantic` (and `fmt`), or None."""
        off = 0
        for a in self.attrs:
            if a.semantic == semantic and (fmt is None or a.fmt == fmt):
                return off
            off += a.size
        return None


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
    normals: list[tuple[float, float, float]]  # empty when the layout has no u8 normal
    end: int = 0  # file offset just past this block


def _parse_layout(s: str) -> Stream:
    attrs = tuple(Attr(t, _SEM.get(t[0], t[0]), int(t[1]), t[2]) for t in s.split("_")
                  if len(t) >= 3 and t[1].isdigit() and t[2] in _FMT)
    return Stream(s, attrs)


def _read_descriptors(data: bytes, pos: int) -> tuple[list[str], int]:
    out = []
    for _ in range(4):
        if pos + 2 > len(data):
            break
        n = struct.unpack_from("<H", data, pos)[0]
        s = data[pos + 2 : pos + 2 + n]
        if not 0 < n <= 64 or not _NAME_RE.fullmatch(s):
            break
        out.append(s.decode())
        pos += 2 + n
    return out, pos


def _strided(stride: int, off: int, fmt: str) -> struct.Struct:
    """Struct reading `fmt` at `off` within one `stride`-byte record (for `iter_unpack`)."""
    return struct.Struct(f"<{off}x{fmt}{stride - off - struct.calcsize('<' + fmt)}x")


def _fills(lo, hi, bbox: Bbox, tol: float) -> bool:
    """Extent lo..hi lies within the padded `bbox` and covers at least half its diagonal."""
    if any(lo[i] < bbox[i] - tol or hi[i] > bbox[i + 3] + tol for i in range(3)):
        return False
    return math.dist(lo, hi) >= 0.5 * math.dist(bbox[:3], bbox[3:]) - tol


def _bbox_tol(bbox: Bbox) -> float:
    """Tolerance scaled by the bbox diagonal (the stored bbox is slightly padded vs the vertices)."""
    return 2e-3 + 0.02 * math.dist(bbox[:3], bbox[3:])


def _decode_block(data: bytes, desc_start: int) -> Block:
    identifier, subgrid_count, vcount, icount = struct.unpack_from("<HHII", data, desc_start - 12)
    descriptors, pos = _read_descriptors(data, desc_start)
    streams = [_parse_layout(s) for s in descriptors if s != "None" and _DESC_RE.search(s.encode())]
    if not streams:
        raise ValueError("no vertex streams")
    # Cheap gate before the buffer search: rejects payloads that merely contain a descriptor-like run.
    if not (0 < vcount <= 2_000_000 and 3 <= icount <= 20_000_000 and icount % 3 == 0):
        raise ValueError("implausible vertex/index counts")

    bbox = struct.unpack_from("<6f", data, pos)
    if not all(abs(b) < _MAX_COORD for b in bbox) or any(bbox[i] > bbox[i + 3] for i in range(3)):
        raise ValueError("implausible bounding box")
    region = pos + 24

    s0 = streams[0]
    poff = s0.offset("POSITION")
    if poff is None:
        raise ValueError("stream has no position")
    vsize = vcount * sum(s.stride for s in streams)
    found = _locate_buffers(data, region, vcount, icount, s0, poff, bbox, vsize)
    if found is None:
        raise ValueError("vertex/index buffers not found")
    vtx, idx_off, indices = found

    stride = s0.stride
    vbuf = memoryview(data)[vtx : vtx + vcount * stride]
    positions = list(_strided(stride, poff, "3f").iter_unpack(vbuf))
    uoff = s0.offset("TEXCOORD", fmt="F")
    uvs = list(_strided(stride, uoff, "2f").iter_unpack(vbuf)) if uoff is not None else [(0.0, 0.0)] * vcount
    noff = s0.offset("NORMAL", fmt="B")
    normals = ([(x / 127.5 - 1.0, y / 127.5 - 1.0, z / 127.5 - 1.0)
                for x, y, z in _strided(stride, noff, "3B").iter_unpack(vbuf)] if noff is not None else [])

    subgrids = _read_subgrids(data, region, subgrid_count, min(vtx, idx_off))
    end = max(vtx + vsize, idx_off + icount * 2) + _TRAILER_SIZE
    return Block(identifier, subgrids, vcount, icount, streams,
                 bbox[:3], bbox[3:], indices, positions, uvs, normals, end)


def _bbox_at(mv: memoryview, off: int, pos: struct.Struct, stride: int, vcount: int,
             bbox: Bbox, tol: float) -> bool:
    """True when `vcount` positions at `off` fit inside `bbox` and fill most of it (early-exit)."""
    lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = bbox
    mnx = mny = mnz = math.inf
    mxx = mxy = mxz = -math.inf
    for x, y, z in pos.iter_unpack(mv[off : off + vcount * stride]):
        if not (abs(x) <= _MAX_COORD and abs(y) <= _MAX_COORD and abs(z) <= _MAX_COORD):
            return False  # also rejects NaN
        mnx, mny, mnz = min(mnx, x), min(mny, y), min(mnz, z)
        mxx, mxy, mxz = max(mxx, x), max(mxy, y), max(mxz, z)
        if (mnx < lo_x - tol or mny < lo_y - tol or mnz < lo_z - tol
                or mxx > hi_x + tol or mxy > hi_y + tol or mxz > hi_z + tol):
            return False
    return _fills((mnx, mny, mnz), (mxx, mxy, mxz), bbox, tol)


def _index_side(data: bytes, off: int, lo: int, vcount: int, icount: int) -> tuple[list[int], int] | None:
    """If `icount` u16 at `off` (not before `lo`) are all < vcount, return (indices, degenerate_tris)."""
    if off < lo or off + icount * 2 > len(data):
        return None
    vals = struct.unpack_from(f"<{icount}H", data, off)
    if max(vals) >= vcount:
        return None
    degen = sum(1 for a, b, c in zip(vals[0::3], vals[1::3], vals[2::3]) if a == b or b == c or a == c)
    return list(vals), degen


def _locate_buffers(data: bytes, start: int, vcount: int, icount: int, s0: Stream, poff: int,
                    bbox: Bbox, vsize: int) -> tuple[int, int, list[int]] | None:
    """Find (vtx_off, idx_off, indices): the vertex offset reproducing `bbox` with an in-range,
    mostly-non-degenerate index buffer directly before or after the vertex streams."""
    stride = s0.stride
    tol = _bbox_tol(bbox)
    mv = memoryview(data)
    pos = _strided(stride, poff, "3f")
    last = min(len(data) - vcount * stride, start + icount * 2 + 4096)
    max_degen = max(1, icount // 6)  # tolerate up to half-degenerate (strips, padding)
    for off in range(start, last + 1, 2):
        if not _bbox_at(mv, off, pos, stride, vcount, bbox, tol):
            continue
        sides = [(cand, side) for cand in (off - icount * 2, off + vsize)
                 if (side := _index_side(data, cand, start, vcount, icount))]
        if sides:
            idx_off, (indices, degen) = min(sides, key=lambda s: s[1][1])
            if degen <= max_degen:
                return off, idx_off, indices
        # bbox matched without a usable index buffer: coincidental fit, keep scanning
    return None


def _read_subgrids(data: bytes, region: int, count: int, limit: int) -> list[tuple[int, int, int, int]]:
    """Submesh ranges (start_index, index_count, start_vertex, vertex_count) from 32-byte records at
    `region`; empty when the table would run past `limit` (first buffer) or the record size differs."""
    if not count or region + count * _SUBGRID_SIZE > limit:
        return []
    return [struct.unpack_from("<8I", data, region + i * _SUBGRID_SIZE)[4:] for i in range(count)]


def parse(data: bytes) -> list[Block]:
    """Decode every mesh block in a `.MESSIAH` payload. Raises if it is not a mesh container."""
    if data[:8] != MAGIC:
        raise ValueError("not a .MESSIAH payload")
    first = _DESC_RE.search(data)
    if not first:
        raise ValueError("no vertex descriptor (not a mesh sub-type)")
    blocks: list[Block] = []
    pos = first.start() - 2
    while True:
        try:
            block = _decode_block(data, pos)
        except (ValueError, struct.error) as e:
            if not blocks:
                raise ValueError(f"no decodable mesh block: {e}") from None
            break
        blocks.append(block)
        # Later blocks are found by scanning from just before the trailer; a spurious descriptor
        # match fails the buffer search and ends the walk.
        nxt = _DESC_RE.search(data, block.end - 64)
        if not nxt or nxt.start() - 2 <= pos:
            break
        pos = nxt.start() - 2
    return blocks


def bbox_matches(block: Block) -> bool:
    """Positions sit inside the (slightly padded) stored bbox and fill most of it."""
    if not block.positions:
        return False
    bbox = (*block.bbox_min, *block.bbox_max)
    lo = tuple(min(p[i] for p in block.positions) for i in range(3))
    hi = tuple(max(p[i] for p in block.positions) for i in range(3))
    return _fills(lo, hi, bbox, _bbox_tol(bbox))


def to_obj(blocks: list[Block], name: str) -> str:
    lines, base = [f"# {name}", "# decoded by wwm-mesh"], 0
    for bi, b in enumerate(blocks):
        lines.append(f"o block{bi}")
        lines.extend(f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in b.positions)
        lines.extend(f"vt {u:.6f} {1.0 - v:.6f}" for u, v in b.uvs)
        lines.extend(f"vn {x:.4f} {y:.4f} {z:.4f}" for x, y, z in b.normals)
        corner = "{0}/{0}/{0}" if b.normals else "{0}/{0}"
        idx = b.indices
        for t in range(0, len(idx) - 2, 3):
            lines.append("f " + " ".join(corner.format(i + 1 + base) for i in idx[t : t + 3]))
        base += len(b.positions)
    return "\n".join(lines) + "\n"


def _load_payload(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:4] in (b"EZST", b"ZSTD"):
        data = ezst_decode(data)
    return data


def _subtype_label(data: bytes) -> str:
    subtype = data[8:12]
    return subtype.decode("latin1") if subtype.isalpha() else f"int{struct.unpack_from('<I', data, 8)[0]}"


def cmd_inspect(args: argparse.Namespace) -> int:
    data = _load_payload(Path(args.file))
    try:
        blocks = parse(data)
    except ValueError as e:
        label = _subtype_label(data) if data[:8] == MAGIC else args.file
        print(f"{label}: {e}", file=sys.stderr)
        return 1
    print(f"sub-type {_subtype_label(data)}; {len(blocks)} mesh block(s)")
    for i, b in enumerate(blocks):
        layouts = ", ".join(f"{s.layout}({s.stride}B)" for s in b.streams)
        print(f"  block {i}: {b.vertex_count} verts, {b.index_count // 3} tris, "
              f"{len(b.subgrids)} subgrid(s); streams [{layouts}]")
        print(f"    bbox min {tuple(round(x, 3) for x in b.bbox_min)} "
              f"max {tuple(round(x, 3) for x in b.bbox_max)}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    try:
        blocks = parse(_load_payload(Path(args.file)))
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    out = Path(args.output)
    out.write_text(to_obj(blocks, Path(args.file).name))
    v = sum(b.vertex_count for b in blocks)
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
    e.set_defaults(func=cmd_export)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
