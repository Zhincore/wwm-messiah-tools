# File ids

Every file in the VFS is addressed by a 64-bit id derived from its path. Packs and indexes store
only ids, never names. Implementation: `file_id()` in `tools/wwm_harvest_paths.py`.

```
id = (MurmurHash3_x86_32(path, seed 0) << 32) | lo32
```

- `path`: the full VFS path as UTF-8, forward slashes, case-sensitive, extension included.
- `lo32`: computed from the path **without** its extension. The extension is cut at the last `.`
  only when that dot comes after the last `/`.

## lo32: tail signature
With `b` = the stripped path and `n` its length:

| n | lo32 |
|---|---|
| 0 | `0` |
| 1 | `b[0] << 24 \| n` |
| 2 | `b[1] << 24 \| b[0] << 16 \| n` |
| ≥ 3 | `b[n-1] << 24 \| b[n-2] << 16 \| b[n-3] << 8 \| (n & 0xFF)` |

So the low byte is the length (mod 256) and the upper three bytes are the last three characters,
last character on top. Read as little-endian bytes on disk: `len, c[n-3], c[n-2], c[n-1]`.

## lo32: GUID fast path
When the stripped path is 39 or 41 bytes long and byte 2 is `/`, `lo32` is the big-endian u32 of
bytes 3..6, i.e. the first four characters after `xx/`. Every real path seen has the 39-byte shape
`xx/<36-char GUID>`: `xx` is the first two characters of the GUID, the GUID is lowercase with dashes,
and the extension is absent, `.6` or `.1` (see `resource-names.md`). The 41-byte case has not been
observed, and whether the engine tests the length before or after stripping the extension is open
(both give the same result for these paths).

## Encodings of the same id
| Where | Form |
|---|---|
| `.mpkinfo` entry | `u32 lo32, u32 hash` (little-endian, lo32 first) |
| patchlist msgpack keys | unsigned u64 |
| `Patch.mpkdb` `v0` | signed i64 (subtract 2^64 when ≥ 2^63) |

## Recovering names
- A candidate path must match `lo32` (last three characters and length) before hashing; that filter
  is nearly free and removes almost everything.
- Lua: each chunk carries its `source` debug field; `LuaText/<source path>` hashes to the entry id.
  All 53k script entries are named this way.
- Cocos UI, animations, video, audio events: paths appear in clear text inside csb, Lua and JSON
  payloads; `tools/wwm_harvest_paths.py` harvests and hashes them.
- Textures, meshes, materials and other engine resources are GUID-keyed; the repository names them
  (`resource-names.md`, `tools/wwm_repository.py`).
