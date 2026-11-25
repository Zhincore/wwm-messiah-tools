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

A mesh container holds one or more **blocks** (LODs / parts), laid out back to back:

```
u16 identifier            (0 for a normal mesh; wq223@reshax: 2 for this game)
u16 subgrid_count
u32 vertex_count
u32 index_count
4 × descriptor string     u16 length + ASCII; vertex-stream layouts, then "None" padding
bbox                      6 × f32  (min x y z, max x y z)
subgrid table             subgrid_count × 32 bytes
index buffer              index_count × u16
vertex streams            planar: one contiguous block per layout, in order
trailer                   52 bytes (end bounding data)
```

`vertex_count` and `index_count` are the two `u32` immediately before the first descriptor, so the
parser anchors on the descriptor and reads back; `identifier`/`subgrid_count` are the `u16` pair
before those. (Larger `HRMD`/`SEMC` headers carry extra fixed fields ahead of this, which the
anchor-and-read-back approach skips over.)

The index buffer precedes the vertex data. A subgrid (submesh) record's last four `u32` are
`start_index, index_count, start_vertex, vertex_count` — the draw ranges into the shared buffers.

## Vertex-stream layouts

Each descriptor string is a vertex **stream** (buffers are planar, not interleaved): `ATTR_ATTR_…`,
each `ATTR = <Semantic><Count><Format>`.

- Semantic: `P` position, `N` normal, `T` texcoord (or tangent in the 2nd stream), `W` blend-weight,
  `I` blend-index, `B` binormal, `C` colour, `G` tangent.
- Format: `F` float32, `H` float16 (half), `B` uint8, `U` uint32. Attribute size = count × sizeof(format).

Common layouts:

| layout | stride | meaning |
|---|---|---|
| `P3F_N4B_T2F` / `P3F_N4B_T2F_T2F` | 24 / 32 | position f32×3, normal u8×4, uv0 (+uv1) f32×2 |
| `T4H_B4H` | 16 | tangent half×4, binormal half×4 |
| `W4B_I4B` | 8 | skin weights u8×4, bone indices u8×4 (skinned meshes only) |

The stored bbox gives a decode self-check: the decoded positions must reproduce it
(`wwm-mesh` reports `self-check ok`/`MISMATCH`, and `export` refuses a mismatch without `--force`).

## Tool

```
wwm-mesh inspect <file>            # sub-type, blocks, counts, layouts, bbox + self-check
wwm-mesh export  <file> -o out.obj # positions + UV + normals + faces, one object per block
```

`<file>` is a raw EZST block or an already-decoded payload (e.g. from `wwm-files export <path>`).
OBJ carries positions, UVs and (approximate, from the u8 normal) normals; tangents/binormals and
skin weights are parsed but not written (OBJ has no slot for them).

## Status / open

- int-`2`/`8` (static meshes, the bulk) and the first block of `HRMD`/`SEMC` decode cleanly
  (bbox self-check passes, zero degenerate triangles across tested samples).
- `SEMC` payloads can carry a large trailer after the mesh with no second vertex descriptor
  (purpose not yet identified; not needed for the geometry).
- Normal/tangent exact packing (sign convention, the 4th component) is approximate.

Reconciles the partial ResHax findings (topic 203, MeshToOBJ): header = magic + pad + length,
descriptor strings are length-prefixed, `index_count × u16`, vertex stride from the descriptors.
