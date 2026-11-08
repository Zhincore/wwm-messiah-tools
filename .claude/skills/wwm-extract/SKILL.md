---
name: wwm-extract
description: Extract textures/images or decode obfuscated text from a Where Winds Meet install with tools/wwm_textures.py and tools/wwm_decode_text.py. Use when asked to pull assets, textures, PNGs, DDS, or to read HEX_/HEXB_ files, patchlists, or game logs.
---

# WWM asset extraction

## Setup (once)
```bash
python3 -m venv .venv && .venv/bin/pip install pillow pycryptodome zstandard msgpack
```

## Textures
1. Check space: full install ≈ 60 GB of PNG/DDS. `df -h` the target drive; never write into the repo or the game drive if it is tight.
2. Inventory first (fast, read-only):
   `.venv/bin/python tools/wwm_textures.py scan <install_root>`
3. Extract:
   `.venv/bin/python tools/wwm_textures.py extract <install_root> -o <out> -j <cores-2>`
   - `--sources res,lt,cache,db` to restrict index families
   - `--container 'Patch1[0-9]\.mpk'` to restrict packs (regex on file name)
   - `--limit N` for a smoke run, `--dds` to always keep DDS
   - `--resume` after any interruption (appends to `manifest.csv`, skips finished packs)
4. Run long extractions in the background; log to a file; report milestones only.
5. Verify visually: `magick montage -geometry 160x160+4+4 -tile 6x <out>/db0/*.png | head` on a sample.

Output: `<out>/<source>/<file_id>.png` (top mip), `.dds` when Pillow cannot decode
(RGBA16F etc.), `manifest.csv` with status per entry. Statuses: `png`, `dds_only`,
`dup` (identical content elsewhere), `no_payload` (streamed texture, format 55),
`decode_failed`, `parse_failed`.

## Text files
`.venv/bin/python tools/wwm_decode_text.py <files...> [-o <dir>]`
Sniffs `HEX_` (JSON), `HEXB_` (msgpack), `CLv2` combat logs (`ClientLocalLog/**/*.bin` → JSON lines),
shift-1 logs (`game_account.log`), plain.
`game_account.log` and `avatarinfo.txt` contain account identifiers: do not paste them.

## Gotchas
- `.mpkinfo` files starting with `HEX`/`HEXB` are metadata, not indexes (the tool skips them).
- Packs starting with `EZST` are block packs: index offsets are meaningless on lite installs.
- No filenames exist anywhere; ids are hashes (see README "Indexes").
- Rust `mpk` crate parses indexes only; it does not decode EZST or textures yet.
