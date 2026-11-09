# wwm-messiah-tools — agent notes

Tools for Where Winds Meet (Messiah engine) asset packs. Format details: `README.md`.
External reference for anything not covered there: https://github.com/ArArgon/yysls-re (docs/).

## Layout
- `tools/wwm_textures.py` — texture/image extractor (the main deliverable)
- `tools/wwm_decode_text.py` — HEX_/HEXB_/shift-1 text decoder
- `mpk/` — Rust: v3 `.mpkinfo` parser (`v3_parser.rs`), legacy v1 reader
- `texture/` — Rust: `EPixelFormat` enum; texture decoding not ported yet
- `resources/` — Rust: legacy Diablo Immortal `.repository` code, untouched

## Rules
- Python deps: `pillow pycryptodome zstandard msgpack` in a venv; `cargo build` for Rust.
- Never stat/open per index entry on the game drive: 1.5M entries, slow mount. Resolve containers once per pack.
- Never hardcode pack numbers, file ids or offsets. Resolve through indexes; packs change per build.
- Block packs (start with `EZST`): index offsets are invalid in lite installs, walk blocks. Detection is
  `Pack._index_is_physical` (sampled payload magics), never "starts with EZST" alone: full installs have
  700 MB packs that start with an EZST entry.
- Memory: packs are mmapped, blocks decoded on demand; never `read()` a whole pack or keep decoded blocks.
  Multiprocessing uses `spawn` so workers do not inherit the 2.8M-entry index. Python venv lives in `.venv`.
- Working outputs (harvests, csb dumps, logs) go to `~/.claude/projects/<slug>/work/`, not `/tmp` (lost on reboot).
- Extraction output is large (~50 GB full). Check `df` before running; write outside the repo.
- Game data, extracted assets, manifests: never commit.
- Rust: run `cargo test -p messiah-mpk` after touching `mpk/`.
- Keep comments to what the code does; no change logs in code.
