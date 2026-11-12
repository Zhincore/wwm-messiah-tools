# Patch database, content hash, entry resolution

Format notes behind `wwm-files` (`tools/wwm/files.py`). Observed on a lite install; "confirmed" means
observed in the running engine.

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
changes what the engine loads for that id (confirmed with an EZST animation).

Patcher-class files (v5=1) are the exception: on every launch `patch/patch_patcher.lua` verifies them
against the server's hashes and fetches them again when they differ, so a different row does not
persist. The bootstrap scripts also exist in the base `Resources.mpk`.

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
The patcher is Lua (`LuaText/patch/...`) and runs before the game proper (game executable):

1. `checker.check`: device and shader set-up (`aftercheck`), writes `LocalData/EnterGameFail.tag`.
2. `launcher.fetch_patch_patch` (`patch/patch_patcher.lua`): fetches the server patchlist, verifies every
   patcher-class file (`common.filelist`, `info0.v5 = 1`) against the server hash, refetches mismatches into
   the patch layer, registers only those entries, then unloads every Lua module and reloads. Patcher-class
   scripts therefore heal themselves on each launch.
3. `patch_main.init` → Patcher stages: pre-check, patchlist, load (database validation, verify of the
   `verifying` packs and of the essential list), download, finish. The login loop video is opened here.
   The load stage registers all `Patch.mpkdb` entries with the engine.
4. `Patcher:enter_main` unloads every module except the patcher's context and runs `main.lua`.

Consequence: a file the patcher opens before step 3 registers the database is read from its base pack
whatever the database says (confirmed with the login video).

Per-launch verification (essential list, `essential.txt`, hash checked against `essential_list_md5` in
`meta`) hashes every `LT*.mpk` / `LT*.mpkinfo` as a whole file, plus the HD store files and the
TinyFiles/MpkCached/MpkSorted indexes. `Resources.mpk` is not on it. Any byte changed in an LT pack marks
the pack corrupt and it is downloaded again; patchlist hashes of other files are checked on patch and
repair, so a repair restores overwritten bytes.

Database validation in the load stage runs `PRAGMA integrity_check` and a `GROUP BY` over `info0`/`info1`.
Any SQLite error there counts as an invalid database: `Patch.mpkdb.backup` is restored if it exists
(the finish stage copies and validates it at the end of every completed patch run), otherwise the
database and every patch pack are deleted (full download). Observed on a lite
install under wine on an NTFS (fuseblk) mount: "unable to open database file" from that query on a
healthy database, on every launch with a full database (cause unconfirmed; the game points SQLite's
temp directory at the Patch folder).

## Stored forms
An entry is stored raw (MP4, Wwise audio, texture containers), as `EZST`, or as `ZSTD`:

```
"EZST" u32 raw_size payload     payload[:min(floor16(len), 256)] AES-256-ECB, then one zstd frame
"ZSTD" u32 raw_size payload     same, not encrypted
```
The AES key is fixed (constant in `tools/wwm/codecs.py`). `ezst_encode()` builds either form.

Lua scripts (`LuaText/<path>.lua`) use their own wrapper, with no magic: `u32le decoded_size` + raw
LZ4 block, decoding to a Lua 5.4 chunk with the game's opcode numbering. `wwm-files`
detects it on the original entry and re-wraps new content the same way. LZ4 output cannot be padded to
a given length.

`wwm-files export <install> <vfs_path> <out>` writes the decoded content of the copy the engine resolves
first (patch-database rows before packages); `--stored` keeps the wrapper, `--source` picks another copy.

## How an entry's bytes are resolved
These observations come from changing single entries on a test install with `wwm-files` (every change
journalled and reverted).
- **Database rows.** An `info0` row points at bytes appended to a patch pack; once registered it wins
  over the package copy, with no size constraint. Files opened before registration are not affected.
- **Package bytes.** Neighbouring entries and index offsets are fixed, so stored bytes can only be
  exchanged for a stored form of exactly the same length. For a database-backed entry the row's `v3`
  must match the bytes, or the next check counts it as corrupt. Patcher-class files are re-verified
  against the server patchlist on boot.

### Stored forms of a given exact length
- Raw MP4: append a `free` box of the missing length.
- EZST/ZSTD: compress, then grow the zstd frame to the target without changing what it decodes to.
  The frame's last block gets its last-block flag cleared and empty raw blocks (3 bytes each:
  `00 00 00`, the final one `01 00 00`) are appended. A gap that is not a multiple of 3 is absorbed
  by the frame header: four equivalent headers (with or without a window descriptor byte, content
  size as u32 or u64) give lengths covering every residue modulo 3, so every gap except 1 and 4 bytes fits. Of 113 real EZST
  entries re-encoded this way, 72 fit; the rest (mostly meshes) were stored tighter than zstd level
  22 reaches. A padded animation block was accepted by the engine.

## Not built
- Texture container encoding: the container holds one EZST block per mip plus size fields; only
  decoding is implemented.
- Lite "block packs" (catalogue packs made of compacted EZST blocks) are skipped by the tool.
