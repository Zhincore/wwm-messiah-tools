#!/usr/bin/env python3
"""
Decode Where Winds Meet obfuscated text files.

Detected by content, not by extension:
  HEX_  ... base64 -> XOR(repeating "netease_hex_xor_encrypt_key") -> zlib -> JSON
  HEXB_ ... same transport, payload is msgpack
  shift1    every byte + 1 (e.g. LocalData/game_account.log)
  CLv2      ClientLocalLog/*.bin: u32be length + base64 records, each HEX transport around msgpack
  LZ4 frame LocalData/TextContent/*: compressed text (character preset JSON)
  SQLite    LocalData/LocalDB/*: localdb table, content = msgpack XOR NetEase key

Usage:
  wwm-decode-text FILE [FILE ...]            print decoded content to stdout
  wwm-decode-text FILE ... -o OUT_DIR        write <name>.json / .txt per input
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import zlib
from pathlib import Path

HEX_XOR_KEY = b"netease_hex_xor_encrypt_key"
PREFIXES = ("HEXB_", "HEX_")


def xor_repeat(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def decode_hex(text: str) -> bytes:
    text = text.strip()
    for p in PREFIXES:
        if text.startswith(p):
            text = text[len(p):]
            break
    return zlib.decompress(xor_repeat(base64.b64decode(text), HEX_XOR_KEY))


def to_jsonable(obj):
    """msgpack output uses bytes keys/values; make it JSON-serialisable."""
    if isinstance(obj, dict):
        return {str(to_jsonable(k)): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.hex()
    return obj


def decode_payload(raw: bytes) -> tuple[str, str]:
    """Return (kind, text) for an inflated HEX payload: json, msgpack, or raw."""
    try:
        return "json", json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except Exception:
        pass
    try:
        import msgpack  # optional dependency, only needed for HEXB_ manifests

        obj = msgpack.unpackb(raw, raw=True, strict_map_key=False)
        return "msgpack", json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False)
    except ImportError:
        return "raw", f"<msgpack payload, {len(raw)} bytes; pip install msgpack to decode>"
    except Exception:
        return "raw", raw.decode("utf-8", errors="replace")


def decode_clv2(data: bytes) -> str:
    """
    ClientLocalLog/*.bin: "CLv2" then records of u32 big-endian length + base64 body.
    Each body is the HEX transport (XOR + zlib) around msgpack. Returns JSON lines.
    """
    import msgpack  # noqa: PLC0415

    pos, out = 4, []
    while pos + 4 <= len(data):
        n = int.from_bytes(data[pos : pos + 4], "big")
        body = data[pos + 4 : pos + 4 + n]
        pos += 4 + n
        raw = zlib.decompress(xor_repeat(base64.b64decode(body), HEX_XOR_KEY))
        obj = msgpack.unpackb(raw, raw=True, strict_map_key=False)
        out.append(json.dumps(to_jsonable(obj), ensure_ascii=False))
    return "\n".join(out) + "\n"


def decode_localdb(path: Path) -> str:
    """
    LocalData/LocalDB/* SQLite: table localdb(id, key, scope, content BLOB).
    content = msgpack XORed with the NetEase key (no base64/zlib). Returns JSON lines.
    """
    import sqlite3  # noqa: PLC0415

    import msgpack  # noqa: PLC0415

    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    out = []
    try:
        for rid, key, scope, content in con.execute("select id, key, scope, content from localdb"):
            try:
                value = to_jsonable(msgpack.unpackb(xor_repeat(content, HEX_XOR_KEY), raw=True, strict_map_key=False))
            except Exception as e:  # noqa: BLE001 - keep the row, mark it
                value = {"_undecoded": content.hex(), "_error": str(e)}
            out.append(json.dumps({"id": rid, "key": key, "scope": scope, "value": value}, ensure_ascii=False))
    finally:
        con.close()
    return "\n".join(out) + "\n"


def looks_shift1(data: bytes) -> bool:
    """Shift-1 text: after -1 the head is printable and newlines appear as 0x0b."""
    head = bytes((b - 1) & 0xFF for b in data[:256])
    printable = sum(32 <= c < 127 or c in (9, 10, 13) for c in head)
    return b"\x0b" in data[:4096] and printable >= 0.9 * len(head)


def decode_file(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    head = data[:8].lstrip()
    if any(head.startswith(p.encode()) for p in PREFIXES):
        kind, text = decode_payload(decode_hex(data.decode("ascii")))
        return f"hex:{kind}", text
    if data[:4] == b"CLv2":
        return "clv2", decode_clv2(data)
    if data[:15] == b"SQLite format 3":
        return "localdb", decode_localdb(path)
    if data[:4] == b"\x04\x22\x4d\x18":  # LZ4 frame (LocalData/TextContent/*)
        import lz4.frame  # noqa: PLC0415

        return "lz4", lz4.frame.decompress(data).decode("utf-8", errors="replace")
    if looks_shift1(data):
        return "shift1", bytes((b - 1) & 0xFF for b in data).decode("utf-8", errors="replace")
    return "plain", data.decode("utf-8", errors="replace")


DEFAULT_LOADER = 'pcall(function() load(MPatch.GetRawDataInSystem("LocalData","s.lua"))() end)'
HD_PATCH_JOURNAL = Path.home() / ".local/share/wwm-messiah-tools/hd-patches"


def patch_hotfix(args) -> int:
    """Prepend a loader snippet to cached hotfix code strings in a LocalDB db (auto-run at startup).

    The server still decides which ids execute, but cached code is used without re-validation, so a
    persistent hotfix the server keeps listing will run our prepended loader every launch.
    """
    import sqlite3  # noqa: PLC0415
    import msgpack  # noqa: PLC0415
    from .files import game_running  # noqa: PLC0415

    if args.apply and not args.force and game_running():
        return _err("wwm.exe is running; close the game first (it rewrites LocalDB on save) or pass --force")
    db = args.patch_hotfix
    snippet = args.loader if args.loader is not None else DEFAULT_LOADER
    con = sqlite3.connect(db)
    rows = con.execute("select key, scope, content from localdb where key like 'hotfix\\_%' escape '\\'").fetchall()
    rows = [(k, s, c) for k, s, c in rows if k != "hotfix_meta"]
    if not rows:
        return _err(f"no cached hotfixes in {db} (log in once so the server populates them)")
    decoded = []
    for key, scope, content in rows:
        try:
            code = msgpack.unpackb(xor_repeat(content, HEX_XOR_KEY), raw=False, strict_map_key=False)
        except Exception:
            code = None
        if isinstance(code, str) and snippet not in code:
            decoded.append((key, scope, content, code))
    if not decoded:
        return _err("no eligible hotfix rows (already patched, or none hold a code string)")
    decoded.sort(key=lambda r: len(r[3]))
    targets = decoded if args.all else decoded[:1]
    print(f"{db}: {len(rows)} cached hotfixes; prepending loader to {len(targets)} "
          f"({'all eligible' if args.all else 'smallest'})")
    for key, scope, _content, code in targets:
        print(f"  {key} ({len(code)} B)")
    if not args.apply:
        print("dry run: nothing written (add --apply)")
        return 0
    jdir = Path(args.journal_dir)
    jdir.mkdir(parents=True, exist_ok=True)
    jpath = jdir / f"hotfix_{Path(db).name}.json"
    if jpath.exists():
        return _err(f"a hotfix patch is already journalled ({jpath}); revert it first")
    journal = {"db": str(db), "rows": [{"key": k, "scope": s, "orig": c.hex()} for k, s, c, _ in targets]}
    jpath.write_text(json.dumps(journal))
    for key, scope, _content, code in targets:
        new = snippet + "\n;" + code
        blob = xor_repeat(msgpack.packb(new), HEX_XOR_KEY)
        con.execute("update localdb set content=? where key=? and scope=?", (blob, key, scope))
    con.commit()
    con.close()
    print(f"applied. journal: {jpath}")
    return 0


def revert_hotfix(args) -> int:
    import sqlite3  # noqa: PLC0415
    j = json.loads(Path(args.revert_hotfix).read_text())
    con = sqlite3.connect(j["db"])
    for r in j["rows"]:
        con.execute("update localdb set content=? where key=? and scope=?",
                    (bytes.fromhex(r["orig"]), r["key"], r["scope"]))
    con.commit()
    con.close()
    Path(args.revert_hotfix).rename(Path(args.revert_hotfix).with_suffix(".reverted.json"))
    print(f"reverted {len(j['rows'])} hotfix row(s) in {j['db']}")
    return 0


def _err(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("-o", "--out-dir", type=Path, help="write decoded files here instead of stdout")
    ap.add_argument("--patch-hotfix", metavar="DB_MP", help="prepend a loader to cached hotfix code (auto-run)")
    ap.add_argument("--all", action="store_true", help="patch every eligible hotfix row, not just the smallest")
    ap.add_argument("--loader", help=f"loader snippet to prepend (default: {DEFAULT_LOADER})")
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    ap.add_argument("--journal-dir", default=str(HD_PATCH_JOURNAL))
    ap.add_argument("--force", action="store_true", help="skip the game-running check")
    ap.add_argument("--revert-hotfix", metavar="JOURNAL", help="undo a --patch-hotfix change")
    args = ap.parse_args()
    if args.revert_hotfix:
        return revert_hotfix(args)
    if args.patch_hotfix:
        return patch_hotfix(args)
    if not args.files:
        ap.error("need files to decode, --patch-hotfix, or --revert-hotfix")
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for path in args.files:
        try:
            kind, text = decode_file(path)
        except Exception as e:
            print(f"{path}: FAILED ({e})", file=sys.stderr)
            rc = 1
            continue
        if args.out_dir:
            ext = {"hex:json": ".json", "hex:msgpack": ".json", "clv2": ".jsonl", "localdb": ".jsonl"}.get(kind, ".txt")
            out = args.out_dir / (path.name + ext)
            out.write_text(text, encoding="utf-8")
            print(f"{path} [{kind}] -> {out}")
        else:
            print(f"===== {path} [{kind}]")
            print(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
