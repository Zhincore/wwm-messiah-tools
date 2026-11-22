# HD store (MTable data tables)

`Package/HD/<region>/` holds the game's data tables: `MANIFEST`, `index{n}`, `data{n}`, and
`locale/translate_words_map_<lang>[_diff]`. Reader: `tools/wwm/tables.py` (`wwm-tables`). Verified by decoding all
114,171 tables (10.08 M rows) of a Steam global build.

## Two copies: package and patch directory
Hotfixes write newer copies of some store files to `LocalData/Patch/HD/<region>/`. On a Steam global
build (Nov 2026) these were `MANIFEST`, `index1001`, `index1200`, `data1005`, `data1201`, plus `formula/`
and `locale/`, all larger and newer than the same names in `Package/HD/<region>/`. The game is assumed
to read the patch-directory copy first, as it does for other patched files (not tested for HD).

`wwm-tables` reads one directory. Given `Package/HD/<region>/` alone, it can return rows from before the
latest hotfix. For the current state, read a directory that holds the package files with the patch
directory's files in their place (for example, symlinks). In the one comparison made, `setting_items`
and `gpu_table_data` decoded to the same rows either way.

## Container
All files are `0xDEADBEEF` containers of independently compressed blocks. Each block has a 9-byte
header followed by a zstd frame (see `Container` in `tools/wwm/tables.py`).

## MANIFEST
Payload header: `u16 v1, u16 v2, u32 unk, u32 n_tables, u32 n_shards, u32 n_extern, u32 n_other`.

1. Three string pools (`u16 len` + bytes, 8-aligned), each sorted: `n_tables` table names,
   `n_extern` names (tables stored outside the store: `translate_words_map_*`, formula data),
   `n_other` names (no records; purpose unknown).
2. `n_tables` records of 8 × u32. Record *i* belongs to table name *i*.

   | field | meaning |
   |---|---|
   | u0 | row count |
   | u1 | shard count |
   | u2 | schema pointer (`0xFFFFFFFF` = none) |
   | u3 | patch schema pointer |
   | u4 | 32-bit hash (function unknown) |
   | u5 | 0 |
   | u6 | first shard index (running sum of u1) |
   | u7 | kind: `0x400` / `0x800`, bit 0 = schemaless |

3. `n_shards` × `(u32 ptr, u32 patch_ptr)`.
4. Index file names, then data file names: `u32 count`, then `u16 len` + bytes each.

Pointer = `(block << 16) | index_file_number`. Patch pointers go to `index1001` / `index1200`.

## Index blocks
- **Schema**: `u16 count`, then `count × (u16 len, name)`. A cell's field id is its position here.
- **Shard**: a swiss-table (abseil-style) hash map.
  `u64 capacity, u64 n, u64 seed`, `capacity` control bytes (`< 0x80` = occupied) plus 16 mirrored
  bytes, pad to 8, then `capacity` slots of 16 bytes:
  `u64 key, u16 data_file, u16 block, u16 offset/8, u16 length/8`.
  A patch-shard slot replaces the base row with the same key; length 0 deletes it.

## Rows
`data{file}`, block `block`, bytes `[offset*8, (offset+length)*8)`:

- Schema tables: `u16 cell_count`, then cells `u8 field_id, u8 type, value`.
  `type = (TLV tag << 2) | f`, where `f` holds bits 8-9 of the field id (tables with more than
  256 columns). The value is the TLV body without its tag byte.
- Schemaless tables (kind bit 0): one bare TLV value.

TLV tags: `01-04` u8/u16/u32/u64, `05` f32, `06` f64, `08` string, `09` list, `0a` dict, `0b` null,
`0c` false, `0d` true. As cell type bytes: `04/08/0c/10`, `14`, `18`, `20`, `24`, `28`, `2c`, `30`, `34`.

Notes:
- A data block holds rows of several tables back to back; it cannot be walked as one table.
- Row keys are not unique: they repeat across shards, and grouped tables keep several rows per key.
- The manifest row count matches decoded rows except for a small share of patched tables.

## Locale
`translate_words_map_<lang>` (plus `_diff`, which overrides it) is the same container. Block 0 is a
directory. Text id `t` lives in block `1 + t % (blocks - 1)`, a hash map of the same shape with
slots `u64 id, u32 offset, u32 length`; the offset is relative to the offset field itself.
64-bit values in name/description cells are these ids.

## In-place field edit
`wwm-tables <hd> --patch-field TABLE --where FIELD=VALUE --field NAME --value STR [--apply]` replaces one
row's string cell inside its data block, keeping the exact byte length (the new string is padded with
spaces) so neighbouring rows and shard offsets do not move, then re-zstd's the block to its exact stored
size (empty-block padding, as for EZST). The original block bytes are journalled; `--revert-patch
JOURNAL` restores them. The replacement must be no longer than the current value, and the edited block
must still fit its stored size (a less-compressible edit can fail). HD files are loose, so this writes
the file directly; close the game first (the tool refuses while `wwm.exe` runs).

Which copy to edit, and whether an edit lasts:
- The tool edits the file in the directory it is given. Editing a package file that also has a
  patch-directory copy is presumably shadowed by that copy (see "Two copies" above).
- The patcher's per-launch essential list (`StageFinish:_collect_essential_files`, `patch/patcher_stage/
  stage_finish.lua`) includes patch-directory HD files that the patchlist lists: `HD/MANIFEST`, the
  `HD/data*` / `HD/index*` it finds, `HD/formula/*.ft` and `HD/locale/*_diff`. Package files are not
  on it.
- Unconfirmed: the code lists `HD/<name>` at the patch directory root, while the install keeps these
  files in `HD/<region>/`. `essential.txt` is encrypted, so whether patch-directory HD edits are
  detected is unknown. A repair or patch rewrites either copy.

## Formula files (`formula/*.ft`)
Not MTable containers; decoded, see `formula-ft.md`.
