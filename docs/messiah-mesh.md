# `.MESSIAH` mesh format

`.MESSIAH` is the Messiah engine's generic serialized-object wrapper (standalone EZST blocks).
The first four bytes after the 8-byte magic are a **sub-type**, read either as a small integer or
as a FourCC. `wwm-mesh` handles the mesh-geometry sub-types; the rest are other object kinds.

| sub-type | kind | notes |
|---|---|---|
| int `1` | SkinSkeleton | bone chains (`Bip001 *`, `Bone_*_jnt`), each a 3×4 transform |
| int `3` | Material | name, shader, `key=value` params with GUID texture refs |
| int `0` | texture/import stub | small text (`<name>.png\nsize:…\nfilter:…`) |
| int `2`, `8` / `HRMD` / `SEMC` | **Mesh geometry** | handled by `wwm-mesh` |

A mesh container holds one or more **blocks** (LODs / parts). Each block has a fixed head:

```
u16 identifier            (0 for a normal mesh; wq223@reshax: 2 for this game)
u16 subgrid_count
u32 vertex_count
u32 index_count
4 × descriptor string     u16 length + ASCII; vertex-stream layouts, then "None" padding
bbox                      6 × f32  (min x y z, max x y z) — slightly padded vs the real vertices
```

`vertex_count` and `index_count` are the two `u32` immediately before the first descriptor, so the
parser anchors on the descriptor and reads back; `identifier`/`subgrid_count` are the `u16` pair
before those. (Larger `HRMD`/`SEMC` headers carry extra fixed fields ahead of this, which the
anchor-and-read-back approach skips over.)

After the head come, in some order, a **subgrid table**, the **index buffer** (`index_count × u16`),
and the **vertex streams** (planar: one contiguous block per layout), then a 52-byte trailer. The
subgrid-record size and whether the index buffer precedes or follows the vertices **are not fixed**
across meshes, so a single hard offset table does not work. The decoder instead *locates* the
buffers: the vertex offset is the one whose `vertex_count` positions fall inside the stored bbox and
fill most of it, with an in-range, mostly-non-degenerate index buffer on one side. This is robust to
the layout variants and self-validating (see "self-check" below).

A subgrid (submesh) record's last four `u32` are `start_index, index_count, start_vertex,
vertex_count` — the draw ranges into the shared buffers.

## Vertex-stream layouts

Each descriptor string is a vertex **stream** (buffers are planar, not interleaved): `ATTR_ATTR_…`,
each `ATTR = <Semantic><Count><Format>`.

- Semantic: `P` position, `N` normal, `T` texcoord (or tangent in the 2nd stream), `W` blend-weight,
  `I` blend-index, `B` binormal, `C` colour, `G` tangent.
- Format: `F` float32, `H` float16 (half), `B` uint8, `U` uint32. Attribute size = count × sizeof(format).

Attributes seen across the install: `P3F`, `C4B` (vertex colour), `N4B`, `T2F` (one or two UV sets),
`T4F` (a 4-float UV/extra), in stream 0; `T4H_B4H` (tangent + binormal) in stream 1; `W4B_I4B`
(skin) in stream 2. Common layouts:

| layout | stride | meaning |
|---|---|---|
| `P3F_N4B_T2F` / `P3F_N4B_T2F_T2F` | 24 / 32 | position f32×3, normal u8×4, uv0 (+uv1) f32×2 |
| `P3F_C4B_N4B_T2F…` | +4 | adds a u8×4 vertex colour after the position |
| `T4H_B4H` | 16 | tangent half×4, binormal half×4 |
| `W4B_I4B` | 8 | skin weights u8×4, bone indices u8×4 (skinned meshes only) |

The stored bbox gives a decode **self-check**: the decoded positions must sit inside it (it is padded
slightly) and fill most of it. `wwm-mesh` reports `self-check ok`/`MISMATCH`, and `export` refuses a
mismatch without `--force`, so a mislocated buffer is flagged rather than written as wrong geometry.

## Tool

```
wwm-mesh inspect <file>            # sub-type, blocks, counts, layouts, bbox + self-check
wwm-mesh export  <file> -o out.obj # positions + UV + normals + faces, one object per block
```

`<file>` is a raw EZST block or an already-decoded payload (e.g. from `wwm-files export <path>`).
OBJ carries positions, UVs and (approximate, from the u8 normal) normals; tangents/binormals and
skin weights are parsed but not written (OBJ has no slot for them).

## Sub-type census

Across a sweep of the install (`.MESSIAH` block counts): `int3` material (most common), `int1`
skeleton, `int8` mesh, `int5` skeleton/other, FourCCs `DOMC`/`DOMD`/`ASCC` (non-mesh; no vertex
descriptor), `int2`, `int0` texture stubs, `HRMD`/`SEMC` (large rigged meshes). Only `int8`, `HRMD`,
`SEMC` carry a vertex descriptor and decode as geometry; `int2` does not (different object kind).

## Coverage

On the swept `int8` meshes the self-check passes for ~99.9% (a handful fail to locate buffers and are
rejected, not mis-decoded). `HRMD`/`SEMC` decode including multiple LOD/part blocks.

## Open

- A few mesh variants (small / unusual layouts) still fail buffer location (<0.5%); they are rejected
  by the self-check rather than exported wrong.
- `SEMC` payloads can carry a large trailer after the mesh with no second vertex descriptor
  (purpose not yet identified; not needed for the geometry).
- Subgrid-record first 16 bytes and the trailer's full meaning are not decoded.
- Normal/tangent exact packing (sign convention, the 4th component) is approximate; tangents,
  binormals, vertex colour and skin weights are parsed but not written to OBJ.

Reconciles and extends the partial ResHax findings (topic 203, MeshToOBJ): header = magic + pad +
length, length-prefixed descriptor strings, `index_count × u16`, vertex stride from the descriptors —
and adds the self-locating buffer layout (index before/after, padded bbox) that generic converters miss.
