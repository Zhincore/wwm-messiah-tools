# wwm-messiah-tools

Tools for reading the asset packs of **Where Winds Meet** (燕云十六声, NetEase /
Everstone, Messiah engine). Current focus: extracting textures as PNG/DDS.

Forked from [fangfang1984/netease-messiah-tools](https://github.com/fangfang1984/netease-messiah-tools)
(Alexander Guettler, 2022, MIT), which targeted Diablo Immortal's Messiah v1
packs. The v1 code paths are kept for reference; everything WWM-specific is new.

## Quick start

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e .   # or: python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/wwm-textures scan    /path/to/wwm
.venv/bin/wwm-textures extract /path/to/wwm -o /big/disk/wwm_textures -j 8
# interrupted? continue where it stopped:
.venv/bin/wwm-textures extract /path/to/wwm -o /big/disk/wwm_textures --resume
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
MurmurHash3-32 of the path — see `docs/file-id.md`. Files starting with `HEX_`/`HEXB_`
are wrapped msgpack metadata, not indexes.

`Patch.mpkdb`: `v0` id (i64), `v1` size, `v2` pack, `v3` content hash, `v6` offset — see `docs/patching.md`
(content hash, start-up order, replacing files).

### Block packs
Packs that start with `EZST` hold one EZST block per index entry; `size` is the physical
block size. A "lite" install keeps only a subset of blocks, compacted together, so index
offsets do not apply: walk the blocks in order and match them to entries by size.

### EZST
`"EZST" u32 raw_size payload`. The first `min(floor16(len), 256)` payload bytes are
AES-256-ECB encrypted with a fixed key (constant in `tools/wwm/codecs.py`, from
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
`LocalData/LocalDB/db_*` (SQLite, table `localdb(id,key,scope,content)`): `content` is msgpack XORed
with the same NetEase key, no base64/zlib (chat history, UI/red-dot state, caches).
`LocalData/TextContent/*`: LZ4 frames around JSON (character preset variables).
`Package/Other/{enc_c,enc_g,dev_i}.bin`: `HEX_` around plain text / JSON (CPU/GPU block and tier lists).
`wwm-decode-text` handles all of these by sniffing content.

### UI layer (Cocos2d-x)
UI is Cocos Studio: `UIScript/*.csb` widget trees (FlatBuffers, stock `CSParseBinary` schema plus a
`ProjectNodeOptions` override vector and a `Splendor` shader-effect node), `UIScript/plist/*.plist`
sprite atlases, `SpineNode` for Spine animations. Asset paths inside csb/Lua payloads are cleartext,
so hashing them (MurmurHash3-32 + tail signature, `docs/file-id.md`) names index entries.
Atlas textures are GUID-keyed; the repository maps `ui/<name>_0_0_ui` to them (below).

### Resource repository (names → GUIDs)
Engine resources live at `xx/<guid>[.6]` (`xx` = first two characters of the GUID; `.6` = texture). Two
files hold the catalogue: a names file (folders, type names, index roots, block table) and a data file
(index pages plus LZ4 blocks of 64 records: GUID, name, folder, type, dependency GUIDs). 1.38 M records;
`wwm-repository` dumps them with file ids. Layout: `docs/resource-names.md`.

### Lua
Scripts are `LuaText` blobs: `u32 decoded_size` + raw LZ4 block → stock Lua 5.4 chunk whose opcode
table has 17 inserted opcodes (table in `tools/wwm/lua.py`). `wwm-lua` unpacks, remaps to stock
numbering and writes `.luac` named from the chunk's `source` field (`hexm/...`, `Sunshine/...`);
pass `--unluac unluac.jar` to decompile to `.lua`. Differences from stock 5.4 that a decompiler must
know: `MMBIN*` C operand is the metamethod index + 1 (not an opcode), generic `for` reserves 5 internal
registers, `NEWTABLE` variants have no `EXTRAARG`. `tools/unluac-messiah.patch` applies these to unluac
(viruscamp/unluac mirror, commit 9c59af4); build it and run with `-Dunluac.tfor54slots=5` (`wwm-lua` does).

### Data tables (HD store)
`Package/HD/<region>/`: `MANIFEST`, `index{n}`, `data{n}` hold ~114k named tables (`MTable`), read by
`wwm-tables`. All three are `0xDEADBEEF` containers of zstd blocks. Full layout: `docs/hd-store.md`.
- MANIFEST: table names + 32-byte records `rows, shard_count, schema_ptr, patch_schema_ptr, hash, 0,
  first_shard, kind`, then a shard array of `(ptr, patch_ptr)`; `ptr = (block << 16) | index file number`.
- Schema block: field-name list; a cell's field id is its position.
- Shard block: swiss-table hash map `row key → (data file, block, offset/8, length/8)`; patch shards
  (files 1001, 1200) override base shards per key, length 0 deletes the row.
- Row: `u16 cell_count` + cells `(u8 field, u8 type, value)`, `type = (TLV tag << 2) | f` (`f` = bits 8-9 of
  the field id), value = TLV body; schemaless tables (kind bit 0) store a bare TLV value.
- TLV tags: `01-04` u8/u16/u32/u64, `05` f32, `06` f64, `08` string, `09` list, `0a` dict, `0b` null,
  `0c` false, `0d` true. Row keys are not unique (repeat across shards and inside grouped tables); the manifest row
  count can differ slightly for patched tables.
- Text columns hold 64-bit text ids; `locale/translate_words_map_<lang>[_diff]` maps them to strings
  (shard = `1 + id % (blocks - 1)`), `--locale en` resolves them.

```bash
.venv/bin/wwm-tables <install>/Package/HD/oversea --list '^chat_'
.venv/bin/wwm-tables <install>/Package/HD/oversea --table chat_emotion_frame_data --locale en
.venv/bin/wwm-tables <install>/Package/HD/oversea --dump OUT --locale en   # ~1.5 GB JSON
```

### Rich text / chat markup
See `docs/chat-markup.md` (tag grammar, components, colour codes, emotes).

## Layout
Python package `wwm` in `tools/wwm/` (`pyproject.toml`); each tool is a console script
`wwm-<name>` and also runs as `python -m wwm.<module>`.
- `wwm.ids` / `wwm.codecs` / `wwm.vfs` — shared layers: file id and content hash; EZST/ZSTD/LuaText
  wrappers; index sources (`collect_entries`), `Entry`, `Pack` (mmapped packs, block-pack walking)
- `wwm-textures` — texture/image extractor
- `wwm-texture-index` — header-only texture census
- `wwm-decode-text` — decoder for `HEX_`/`HEXB_` wrappers, SQLite LocalDB, CLv2 logs
- `wwm-harvest` — harvest cleartext asset paths from payloads, hash them to file ids, capture csb
- `wwm-csb` — Cocos csb decoder (`cocos_csb_schema.json` generated from cocos2d-x headers)
- `wwm-ui-map` — UI asset map: csb trees, texts, timelines, atlases, recovered names
- `wwm-atlas` — cut plist atlas frames, build GIFs from frame sequences
- `wwm-lua` — Lua bytecode dump (LuaText → stock 5.4 → optional unluac); `tools/unluac-messiah.patch`
- `wwm-build-luac` — build `lua`/`luac` 5.4 with the game's opcode numbering; reads and lists game
  bytecode, but its compiler output is not game-compatible (generic-for slots, metamethod indexes,
  list/dict constructors; see the module docstring)
- `wwm-tables` — HD store data tables (MANIFEST/index/data) + locale text
- `wwm-repository` — resource repository: name ↔ GUID ↔ file id, types, dependencies
- `wwm-files` — `export` writes a file's decoded content as the engine resolves it (patch db before
  packages), ready to edit and feed back. Experimental: replace one VFS file (`override`), either through a `Patch.mpkdb` entry or
  `--in-place` in its pack; EZST/ZSTD entries are re-compressed (in place: to the exact stored size),
  LuaText entries re-wrapped; everything is journalled; `revert` restores one file, `revert-all` every
  override of the install. Confirmed in game (lite install): in place, a raw MP4 (login video) and an
  EZST animation padded with empty zstd blocks; through `Patch.mpkdb`, an EZST animation whose existing
  row was repointed. Files the patcher opens before it registers patch entries (the login video) only
  respond to the in-place route.
- `tests/` — pytest on synthetic data: file ids, codecs, block packs, override/export/revert round trips
- `docs/` — format notes: `file-id.md`, `hd-store.md`, `patching.md`, `resource-names.md`, `chat-markup.md`
- `mpk/` — Rust: v1 reader (Diablo Immortal era) and v3 `.mpkinfo` parser
- `texture/` — Rust: EZST codec, texture container parser, PNG/DDS conversion
  (`cargo run -p messiah-texture -- <payload files or dir> -o OUT [--dds]`)
- `resources/` — Rust: v1 `.repository` handling (Diablo Immortal)

## Credits
- Alexander Guettler — original netease-messiah-tools (MIT)
- [ArArgon/yysls-re](https://github.com/ArArgon/yysls-re) — EZST key, file-id hash, VFS model, `Patch.mpkdb` semantics, Lua opcode table (the parts this repo's tools rely on are restated in `docs/`)
- ResHax community threads on WWM `.mpk` analysis

Research/educational use only. Respect the game's terms of service.
