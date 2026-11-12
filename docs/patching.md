# Patch database, content hash, replacing files

Format notes behind `wwm-files` (`tools/wwm/files.py`). Observed on a lite install; "confirmed" means
checked in game.

## Patch.mpkdb
`LocalData/Patch/Patch.mpkdb` is SQLite. Tables `info0` (base rows) and `info1` (appended updates)
share one shape:

| column | meaning |
|---|---|
| v0 | file id, **signed** i64 (in `info1`, the id XORed with `INFO1_XOR_KEY` = 1234927562014348681) |
| v1 | stored size in bytes |
| v2 | pack number: bytes live in `Patch{v2}.mpk` (patcher files sit in pack 1) |
| v3 | content hash of the stored bytes (below), signed i64 |
| v4 | flag, 0 on ordinary `info0` rows |
| v5 | patcher-file flag: 1 marks patcher/bootstrap scripts (`patch_main`, `patcher`, `launcher`, …), else 0 |
| v6 | offset inside the pack |

`info0` holds foreground files, `info1` background/deferred ones (its `v0` is obfuscated with the XOR
key above). v5 comes from the engine's patcher-file set (`INDEX_PATCHER_FILE`); the patcher reads those
rows apart (`select * from info0 where v5 <> 1 OR v2 <> 1`).

The engine reads these rows directly. Repointing a row at other bytes (new pack, offset, size, hash)
changes what the game loads for that id: confirmed with an EZST animation.

Patcher-class files (v5=1) are the exception: an `info0` override of one does not survive a launch. On
boot the essential/patcher hash verify finds the mismatch and clears the patch-db state, dropping the
row. The bootstrap scripts also exist in the base `Resources.mpk`, so the engine still loads a copy.

## Content hash (v3, patchlist hash64)
```
chunk = 2 MiB if size >= 4 MiB else 32 MiB
h = 0
for each chunk c of the stored bytes:
    a, b = MurmurHash3_x64_128(c, seed 0)    # two u64 halves
    h ^= a ^ b
```
Reproduced on 20 real rows. `content_hash()` in `tools/wwm/ids.py`.

## Start-up order
The patcher is Lua (`LuaText/patch/...`) and runs before the game proper:

1. Load stage: opens the packs and verifies the "essential" list (the `LT{n}` packs, which hold the
   scripts).
2. The login screen appears; its loop video is opened here.
3. Finish stage: patch entries from `Patch.mpkdb` are registered with the engine.

Consequence: a file the patcher opens before step 3 is read from its base pack whatever the database
says. A database override of the login video has no effect; overwriting its bytes in the base pack
does (confirmed). Per-launch verification covers the essential list only; patchlist hashes of other
files are checked on patch and repair, so a repair restores overwritten bytes.

## Stored forms
An entry is stored raw (MP4, Wwise audio, texture containers), as `EZST`, or as `ZSTD`:

```
"EZST" u32 raw_size payload     payload[:min(floor16(len), 256)] AES-256-ECB, then one zstd frame
"ZSTD" u32 raw_size payload     same, not encrypted
```
The AES key is fixed (constant in `tools/wwm/codecs.py`). `ezst_encode()` builds either form.

Lua scripts (`LuaText/<path>.lua`) use their own wrapper, with no magic: `u32le decoded_size` + raw
LZ4 block, decoding to a Lua 5.4 chunk with the game's opcode numbering. `wwm-files`
detects it on the original entry and re-wraps the replacement, which may be bytecode or plain source
(whether the engine's loader accepts source text is untested). LZ4 cannot be padded, so in place
needs an exact stored size; use the database route.

`wwm-files export <install> <vfs_path> <out>` writes the decoded content of the copy the engine resolves
first (patch-database rows before packages); `--stored` keeps the wrapper, `--source` picks another copy.
Edit that file and pass it to `wwm-files override`, which re-wraps it like the original.

## Two ways to replace a file
- **Database route.** Append the new stored bytes to a patch pack and insert or repoint the `info0`
  row. No size limit; base packs untouched. Does not reach files opened before registration.
- **In place.** Overwrite the stored bytes inside the pack. The new stored form must be exactly as
  long as the old one, because neighbours and index offsets do not move. When the overwritten entry is
  itself a patch-database row (a `Patch{n}.mpk` pack targeted via `db0`/`db1`), its `v3` content hash
  is updated in place to match the new bytes, so the row does not go stale; `revert --in-place`
  restores both the bytes and the hash. Base-pack (`res`/`lt`) in-place edits have no row to update,
  but patcher-class files are re-verified against the server patchlist on boot and may be restored.

### Fitting an exact size
- Raw MP4: append a `free` box of the missing length.
- EZST/ZSTD: compress, then grow the zstd frame to the target without changing what it decodes to.
  The frame's last block gets its last-block flag cleared and empty raw blocks (3 bytes each:
  `00 00 00`, the final one `01 00 00`) are appended. A gap that is not a multiple of 3 is absorbed
  by the frame header: four equivalent headers (with or without a window descriptor byte, content
  size as u32 or u64) give lengths covering every residue modulo 3. Of 113 real EZST
  entries re-encoded this way, 72 fit; the rest (mostly meshes) were stored tighter than zstd level
  22 reaches and need the database route. One padded animation is confirmed in game.

## Not built
- Texture replacement: the container holds one EZST block per mip plus size fields, so it has to be
  rebuilt, not just re-wrapped.
- Lite "block packs" (catalogue packs made of compacted EZST blocks) are skipped by the tool.
