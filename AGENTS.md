# wwm-messiah-tools — agent notes

Tools for Where Winds Meet (Messiah engine) asset packs. Format details: `README.md` and `docs/`
(self-contained; record new findings there). Secondary reference: https://github.com/ArArgon/yysls-re (docs/).

## Layout
Python package `wwm` in `tools/wwm/` (`pyproject.toml`, editable install in `.venv`). Tools run as
`.venv/bin/wwm-<name>` or `python -m wwm.<module>`; modules import each other relatively (`from . import vfs`).
- `ids.py` — `file_id()` path hash, `content_hash()` (patch db v3), `signed64()`
- `codecs.py` — EZST/ZSTD encode/decode (exact-size zstd framing), LuaText encode/decode
- `vfs.py` — `collect_entries()` over all index sources, `Entry`, `Pack` (mmap, block-pack walk), `TEX_MAGIC`
- `textures.py` (`wwm-textures`), `texture_index.py` — texture container parse/extract, header census
- `decode_text.py` — HEX_/HEXB_/shift-1/CLv2/LocalDB text decoder
- `harvest.py` — path harvest from payloads, csb capture, manifest matching
- `csb.py`, `ui_map.py`, `atlas.py` — Cocos UI: csb decode, UI map, atlas frame cutting
- `lua.py` — LuaText → stock Lua 5.4 bytecode (+ unluac; needs `tools/unluac-messiah.patch`). Its `.luac`
  output uses stock opcode numbering: never feed it back into the game
- `build_luac.py` — lua/luac 5.4 with the game's opcode numbering
- `tables.py` — HD store data tables: `Store.table(name)`, `Locale` text ids; `--list/--table/--dump`
- `repository.py` — resource repository: `Repository.records()`, `guid_file_id()`; names for GUID-keyed files
- `files.py` (`wwm-files export|override|revert|revert-all`) — `export` is read-only; the rest write to the install (patch pack + `Patch.mpkdb`
  row); only run when the user asks, game closed
- `mpk/` — Rust: v3 `.mpkinfo` parser (`v3_parser.rs`), legacy v1 reader
- `texture/` — Rust: `ezst`, `container`, `convert` (PNG via texture2ddecoder, DDS DX10); CLI converts payload files
- `resources/` — Rust: legacy Diablo Immortal `.repository` code, untouched

## Rules
- Python: `uv pip install --python .venv/bin/python -e '.[dev]'` (deps in `pyproject.toml`); `cargo build` for Rust.
- Never stat/open per index entry on the game drive: 1.5M entries, slow mount. Resolve containers once per pack.
- Never hardcode pack numbers, file ids or offsets. Resolve through indexes; packs change per build.
- Block packs (start with `EZST`): index offsets are invalid in lite installs, walk blocks. Detection is
  `Pack._index_is_physical` (sampled payload magics), never "starts with EZST" alone: full installs have
  700 MB packs that start with an EZST entry.
- Memory: packs are mmapped, blocks decoded on demand; never `read()` a whole pack or keep decoded blocks.
  Multiprocessing uses `spawn` so workers do not inherit the 2.8M-entry index; worker functions must be
  module-level in the package (spawn pickles them by import path).
- Working outputs (harvests, csb dumps, logs) go to `~/.claude/projects/<slug>/work/`, not `/tmp` (lost on reboot).
- Extraction output is large (~50 GB full). Check `df` before running; write outside the repo.
- Game data, extracted assets, manifests: never commit.
- Tests: `.venv/bin/python -m pytest` (synthetic data, no install needed; dev extra `.[dev]`) after touching
  `tools/wwm`; `cargo test -p messiah-mpk` / `-p messiah-texture` after touching those crates.
- Keep comments to what the code does; no change logs in code.
