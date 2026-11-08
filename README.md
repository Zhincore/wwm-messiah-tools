# wwm-messiah-tools

Tools for reading the asset packs of **Where Winds Meet** (燕云十六声, NetEase /
Everstone, Messiah engine). Current focus: extracting textures as PNG/DDS.

Forked from [fangfang1984/netease-messiah-tools](https://github.com/fangfang1984/netease-messiah-tools)
(Alexander Guettler, 2022, MIT), which targeted Diablo Immortal's Messiah v1
packs. The v1 code paths are kept for reference; everything WWM-specific is new.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install pillow pycryptodome zstandard msgpack
.venv/bin/python tools/wwm_textures.py scan    /path/to/wwm
.venv/bin/python tools/wwm_textures.py extract /path/to/wwm -o /big/disk/wwm_textures -j 8
# interrupted? continue where it stopped:
.venv/bin/python tools/wwm_textures.py extract /path/to/wwm -o /big/disk/wwm_textures --resume
```

Output: `<out>/<source>/<file_id>.png` (top mip), `.dds` where Pillow cannot decode the
format, and `manifest.csv` (file id, container, offset, size, format, dimensions,
status). Budget ~60 GB of output for a full install; names are hash ids because the
packs carry no filenames.

## Formats (what is known)

### Indexes
| Index | Container | Notes |
|---|---|---|
| `Resources.mpkinfo` | `Resources.mpk` | install root |
| `LocalData/Patch/LT{n}.mpkinfo` | `LT{n}.mpk` | |
| `LocalData/Patch/{MpkCached,TinyFiles}_*.mpkinfo` | `Patch{pack_id}.mpk` per entry | update packs |
| `LocalData/Patch/Patch.mpkdb` | `Patch{v2}.mpk` | SQLite; `info0` base rows, `info1` appended updates |

`.mpkinfo` v3: `u32 3, u32 count`, then 20-byte entries `u32 id_lo, u32 id_hi, u32 offset,
u32 size, u32 packed` (`pack_id = packed >> 1`, bit 0 = placeholder), 16-byte trailer.
`id_lo` is a "tail signature" of the path (length byte + last 3 bytes), `id_hi` is
MurmurHash3-32 of the path — see yysls-re `file-id.md`. Files starting with `HEX_`/`HEXB_`
are wrapped msgpack metadata, not indexes.

`Patch.mpkdb`: `v0` id (i64), `v1` size, `v2` pack, `v3` content hash, `v6` offset.

### Block packs
Packs that start with `EZST` hold one EZST block per index entry; `size` is the physical
block size. A "lite" install keeps only a subset of blocks, compacted together, so index
offsets do not apply: walk the blocks in order and match them to entries by size.

### EZST
`"EZST" u32 raw_size payload`. The first `min(floor16(len), 256)` payload bytes are
AES-256-ECB encrypted with a fixed key (constant in `tools/wwm_textures.py`, from
[ArArgon/yysls-re](https://github.com/ArArgon/yysls-re)); the rest is plain. The
decrypted payload is one zstd frame. `ZSTD` magic = same header, unencrypted.

### Texture container (`02 02 02 01`)
```
0x00  02 02 02 01
0x04  u8 ?, u8 pixel_format (EPixelFormat, texture/src/pixel_format.rs), u8 mip_count, u8 flags
0x08  u8[4] unknown
0x0c  u16 width, u16 height
0x20  u32 total_size
0x24  u8 01, u8 pixel_format, u8 mip_count, u8 00
0x28  mip records, smallest first:
        u32 blob_size, u16 w, u16 h, u16 depth, u16 pitch, u32 raw_size
        EZST block, payload length = blob_size - 24
0x..  16-byte trailer
```
Observed formats: BC7 (dominant), R8G8B8A8, A16B16G16R16F. Format 55 with `raw_size = 0`
has no local pixel data (streamed/HD asset).

### Other payloads
`.MESSIAH` meshes, `CHAR::ANIM`, particle systems and Lua live inside standalone EZST
blocks; shaders use `ZZZ4` (LZ4) + DXBC; audio is Wwise (`RIFF`/`BKHD`). Not handled here.

### Obfuscated text files
`HEX_`/`HEXB_` prefixed files (`patch_config.json`, `essential.txt`, `avatarinfo.txt`,
`engine_features_*`, patchlists, some `.mpkinfo`): base64 → XOR with the repeating key
`netease_hex_xor_encrypt_key` → zlib → JSON (`HEX_`) or msgpack (`HEXB_`).
`LocalData/game_account.log`: every byte shifted by +1.
`LocalData/ClientLocalLog/<role>/<mode>/*.bin`: `CLv2` magic, then records of u32 big-endian
length + base64 body; each body is the HEX transport around msgpack. Record `[-3, {...}]` is the
session header (logger, session id, space/config), `[-2, ts, [entity_ids], [events]]` is an
event batch; events are `[event_type, tick, ?, subtype, payload...]` (combat/position telemetry).
`tools/wwm_decode_text.py` handles all of these by sniffing content.

## Layout
- `tools/wwm_textures.py` — texture/image extractor (Python)
- `tools/wwm_decode_text.py` — decoder for `HEX_`/`HEXB_` wrappers and shifted logs
- `mpk/` — Rust: v1 reader (Diablo Immortal era) and v3 `.mpkinfo` parser
- `texture/` — Rust: Messiah `EPixelFormat` enum (texture decoding not yet ported)
- `resources/` — Rust: v1 `.repository` handling (Diablo Immortal)

## Credits
- Alexander Guettler — original netease-messiah-tools (MIT)
- [ArArgon/yysls-re](https://github.com/ArArgon/yysls-re) — EZST key, file-id hash, VFS model, `Patch.mpkdb` semantics
- ResHax community threads on WWM `.mpk` analysis

Research/educational use only. Respect the game's terms of service.
