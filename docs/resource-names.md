# Resource names: the repository

Engine resources (textures, meshes, materials, models, particle systems, ...) are stored under
GUID paths, so their file ids cannot be guessed from a readable name. The engine's repository maps
names to GUIDs. Decoder: `tools/wwm/repository.py` (`wwm-repository`). Verified on a Steam global build: 1,376,051 of
1,376,052 records resolve to an indexed file, and a texture identified earlier by its pixels (the chat
emote atlas) comes out under its name with the same file id.

## GUID paths
```
<first two characters of the GUID>/<guid, lowercase, dashed>[<ext>]
e8/e8c8d4c0-0a56-499c-8243-0c93127c7fd3.6
```
- `.6`: `Texture2D` (about 98 %); `.1`: the remaining textures; every other type has no extension.
- The path hashes with the GUID fast path of `file-id.md`: `lo32` is the first four characters of
  the GUID as ASCII. About half of all index entries (1.39 M of 2.8 M) are GUID-keyed.
- GUID strings that appear in Lua (`texture_guid`, ...) resolve the same way.

## The two files
Both are ordinary VFS files with unknown names. Their paths share a stem (same `lo32`: 7 characters
ending in `epo`), which is how the tool finds them: a small file with the signature below plus a
larger sibling. Ids change per build; never hardcode them.

### Names file (about 1.2 MB)
```
u32 1, u32 0, u32 record_count, u8 section_count
sections: u8 id, u32 length, payload
```
| id | payload |
|---|---|
| 0 | folders: `u16 0xFFFF, u32 length`, then `;`-separated folder paths (30k) |
| 1 | types: `u16 length`, then `;`-separated type names (below) |
| 2 | a second index, key unknown: root and page table |
| 3 | name index: root and page table |
| 4 | record blocks: `u16 count`, then `count × (u32 stored_size, u32 offset)` into the data file |
| 5, 6 | bitmaps (16 KiB each; allocation maps, not needed for reading) |

Index section: `u16 n`, `n × u64` first key of each leaf page, `u16 pages`,
`pages × (u32 size, u32 offset)`. Page 0 of each index is blank.

### Data file (about 89 MB)
- Leaf pages (4096 bytes): `u16 count` (341 when full), `count × u64` keys sorted ascending,
  `count × u32` values. `value = (offset_in_block << 16) | block_number`.
- Record blocks: `u32 raw_size` followed by one raw LZ4 block; `stored_size` in section 4 counts
  both. Each block holds 64 records (the last one fewer).

Index keys:
- name index: `file_id("<folder>/<name>")`, the same function as VFS ids (`file-id.md`).
- section 2 index: 64-bit, one per record, function unknown (presumably derived from the GUID, not
  shown). Not MurmurHash3, xxHash, FNV, CRC or MD5 of the raw, mixed-endian or string GUID, nor a
  slice of it, nor any of those over the name.

### Record
```
u16 kind, u16 variant, u8 0
u8  guid[16]                 bytes in string order (no mixed-endian fields)
u16 name_length, name
u16 folder_index             into section 0
u16 type_index               into section 1
u16 dependency_count
u8  dependencies[16] × count GUIDs of other records (a material lists its textures, ...)
```
`kind` is a second class number that follows the type (Texture2D 5, Material 8, Model 10, Mesh 3,
SkinSkeleton 4, CollisionShape 15, LodModel 24, ...). `variant` is 0 or 1 for textures, 0 elsewhere.

Type names, by index: Texture2D, LodModel, Material, Model, Mesh, SkinSkeleton, Texture2DArray,
ClothMesh, ClothModel, ClothColShapeAsset, Flamenco, Ragdoll, CollisionShape, Impostor,
ParticleSystem, DamageMaterial, Destructible, PhysicsMaterial, DestructibleModel, DestructibleMesh,
CurveFloat, CurveColor, CurveVector2, CurveVector3, VisualGraphSystem, Decal, TextureCurve, Prefab,
Texture3D, WaterWave, Wave, Shoreline, Data, PVSCube, NavigateMap, NavigateMapFragment, Animation,
Puppet.

## Naming conventions seen
- UI textures: folder `ui`, name `<sprite or atlas name>_0_0_ui` (the chat emote atlas is
  `ui/dynamic_l_talk_emotion__0_0_ui`).
- Spine stickers: folder `puppet`, a `Puppet` record plus a `<name>_tex` texture it depends on.
- Characters: `C1/<M|F>/...`, `C2/char/...`; environment: `G/Res/...`; effects: `E/...`;
  LOD copies under `lod1/`, `lod2/`, `HLOD/`.

## Usage
```bash
.venv/bin/wwm-repository <install> --out repository.csv
.venv/bin/wwm-repository <install> --find '^puppet/vx_emoji_goose'
.venv/bin/wwm-repository <install> --deps ui/dynamic_l_talk_emotion__0_0_ui
```
The CSV has `file_id, ext, guid, type, path, kind, variant, deps`; `file_id` is empty when no index
of the install lists the file. Listed is not the same as present: a lite install indexes files whose
bytes it has not downloaded.

## Related blobs, not part of the repository
- UI texture list: `u16 length + "ui/<name>_0_0_ui"`, then `u16 width + 4, u16 height + 4` per entry
  (about 63k names, no GUIDs). The names are repository paths.
- A 19 MB list of 560k records `GUID[16], u32 1, f32, u32` (the last usually 2048): about 6 % of
  its GUIDs are files without extension; the rest are not VFS files. Purpose unknown.

## How it was found, and what did not work
- Hashing names into GUIDs (MD5, SHA-1, UUIDv3/v5, MurmurHash3-128) matches nothing: GUIDs are random.
- GUID strings in decompiled Lua gave the path template (4 of 45 resolved at once).
- Scanning large non-GUID files for raw 16-byte UUIDv4 values that resolve to index ids located the
  data file: 1.66 M hits, all past the index pages.
- Earlier public notes describe the same two files as a "token corpus" and a "large table" of
  front-coded dictionary blocks with paged pointers. That reading does not hold: the token list is
  the folder table, the blocks are plain LZ4, and the pointers are `(offset << 16) | block`.
