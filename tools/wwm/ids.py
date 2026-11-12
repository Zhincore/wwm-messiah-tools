"""File ids (path hash used as the index key) and the patch database's content hash."""

from __future__ import annotations

import struct

import mmh3

MASK64 = (1 << 64) - 1
SMALL_FILE = 4 << 20
CHUNK_LARGE_FILE = 2 << 20
CHUNK_SMALL_FILE = 32 << 20


def murmur3_32(data: bytes, seed: int = 0) -> int:
    c1, c2 = 0xCC9E2D51, 0x1B873593
    h = seed
    n = len(data) // 4 * 4
    for i in range(0, n, 4):
        k = struct.unpack_from("<I", data, i)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    k = 0
    tail = data[n:]
    if len(tail) >= 3:
        k ^= tail[2] << 16
    if len(tail) >= 2:
        k ^= tail[1] << 8
    if len(tail) >= 1:
        k ^= tail[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= len(data)
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def strip_ext(p: bytes) -> bytes:
    dot, slash = p.rfind(b"."), p.rfind(b"/")
    return p[:dot] if dot > slash else p


def tail_sig(b: bytes) -> int:
    n = len(b)
    if n == 0:
        return 0
    if n == 1:
        return (b[0] << 24) | n
    if n == 2:
        return (b[1] << 24) | (b[0] << 16) | n
    return (b[n - 1] << 24) | (b[n - 2] << 16) | (b[n - 3] << 8) | (n & 0xFF)


def file_id(path: str) -> int:
    """Index key of a VFS path: (MurmurHash3_x86_32(path) << 32) | tail signature."""
    p = path.encode("utf-8")
    stripped = strip_ext(p)
    if len(stripped) in (39, 41) and len(stripped) > 2 and stripped[2:3] == b"/":
        lo = struct.unpack(">I", stripped[3:7])[0]
    else:
        lo = tail_sig(stripped)
    return (murmur3_32(p) << 32) | lo


def content_hash(data: bytes) -> int:
    """MurmurHash3-128 per chunk, halves XORed, chunks XORed; files >= 4 MiB use 2 MiB chunks."""
    chunk = CHUNK_LARGE_FILE if len(data) >= SMALL_FILE else CHUNK_SMALL_FILE
    acc = 0
    for i in range(0, max(len(data), 1), chunk):
        h = mmh3.hash128(data[i : i + chunk], signed=False)
        acc ^= (h >> 64) ^ (h & MASK64)
    return acc


def signed64(v: int) -> int:
    """Unsigned 64-bit value as SQLite's signed INTEGER."""
    return v - (1 << 64) if v >= 1 << 63 else v
