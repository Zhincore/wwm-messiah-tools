#!/usr/bin/env python3
"""
Decode Where Winds Meet obfuscated text files.

Detected by content, not by extension:
  HEX_  ... base64 -> XOR(repeating "netease_hex_xor_encrypt_key") -> zlib -> JSON
  HEXB_ ... same transport, payload is msgpack
  shift1    every byte + 1 (e.g. LocalData/game_account.log)

Usage:
  wwm_decode_text.py FILE [FILE ...]            print decoded content to stdout
  wwm_decode_text.py FILE ... -o OUT_DIR        write <name>.json / .txt per input
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
            ext = ".json" if kind in ("hex:json", "hex:msgpack") else ".txt"
            out = args.out_dir / (path.name + ext)
            out.write_text(text, encoding="utf-8")
            print(f"{path} [{kind}] -> {out}")
        else:
            print(f"===== {path} [{kind}]")
            print(text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
