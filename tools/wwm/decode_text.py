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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("-o", "--out-dir", type=Path, help="write decoded files here instead of stdout")
    args = ap.parse_args()
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
