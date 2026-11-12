"""
Payload wrappers.

  EZST     "EZST" u32 raw_size, payload: first min(floor16(len),256) bytes AES-256-ECB, then one zstd frame
  ZSTD     "ZSTD" u32 raw_size, one zstd frame (not encrypted)
  LuaText  u32le decoded_size + raw LZ4 block (no magic), decoding to a Lua 5.4 chunk
"""

from __future__ import annotations

import struct

import lz4.block
import zstandard
from Crypto.Cipher import AES

EZST_KEY = struct.pack(
    "<4Q", 0x556A586E32723575, 0x38782F913F152867, 0x2D3B615062536756, 0x6B59703373367639
)
def ezst_decode(blob: bytes, verify: bool = True) -> bytes:
    """Decode one EZST/ZSTD block. Raises on malformed input."""
    magic = blob[:4]
    if magic not in (b"EZST", b"ZSTD") or len(blob) < 8:
        raise ValueError("not an EZST block")
    raw_size = struct.unpack_from("<I", blob, 4)[0]
    payload = blob[8:]
    if magic != b"EZST":
        return _zstd(payload, raw_size, verify)
    # The encrypted prefix is floor16(true payload length) capped at 256. When the blob
    # carries trailing bytes of unknown length, the true length is unknown, so retry
    # with shorter prefixes until the frame decodes.
    n = min(len(payload) // 16 * 16, 256)
    aes = AES.new(EZST_KEY, AES.MODE_ECB)
    last_err: Exception | None = None
    while n >= 0:
        try:
            return _zstd((aes.decrypt(payload[:n]) if n else b"") + payload[n:], raw_size, verify)
        except Exception as e:  # noqa: BLE001 - try the next prefix length
            last_err = e
            n -= 16
    raise ValueError(f"EZST decode failed: {last_err}")


ZSTD_FRAME_MAGIC = b"\x28\xb5\x2f\xfd"
ZSTD_EMPTY_BLOCK = b"\x00\x00\x00"
ZSTD_LAST_EMPTY_BLOCK = b"\x01\x00\x00"
ZSTD_MAX_WINDOW_LOG = 27


def _zstd_split(data: bytes, level: int) -> tuple[bytes, bytes, int]:
    """Compress to one frame; return (frame header, blocks, offset of the last block's header)."""
    frame = zstandard.ZstdCompressor(level=level, write_content_size=True, write_checksum=False).compress(data)
    fhd = frame[4]
    single, dict_bytes, fcs_flag = fhd >> 5 & 1, (0, 1, 2, 4)[fhd & 3], fhd >> 6
    pos = 5 + (0 if single else 1) + dict_bytes + (1 if single else 0, 2, 4, 8)[fcs_flag]
    body = frame[pos:]
    at = 0
    while True:
        h = int.from_bytes(body[at : at + 3], "little")
        if h & 1:
            return frame[:pos], body, at
        at += 3 + (1 if (h >> 1) & 3 == 1 else h >> 3)


def _zstd_headers(content_size: int) -> list[bytes]:
    """Valid frame headers for this content size; their lengths cover every residue modulo 3."""
    window_log = max(10, (content_size - 1).bit_length())
    headers = [ZSTD_FRAME_MAGIC + b"\xa0" + struct.pack("<I", content_size)]            # single segment, u32 size
    if window_log <= ZSTD_MAX_WINDOW_LOG:
        window = bytes([(window_log - 10) << 3])
        headers.append(ZSTD_FRAME_MAGIC + b"\x80" + window + struct.pack("<I", content_size))  # window, u32 size
        headers.append(ZSTD_FRAME_MAGIC + b"\xc0" + window + struct.pack("<Q", content_size))  # window, u64 size
    headers.append(ZSTD_FRAME_MAGIC + b"\xe0" + struct.pack("<Q", content_size))        # single segment, u64 size
    return headers


def zstd_frame(data: bytes, size: int | None = None, level: int = 19) -> bytes:
    """
    One zstd frame holding `data` (content size, no checksum, like the game's own frames).
    With `size`, the frame is exactly that long: a frame header of suitable length is chosen and
    empty raw blocks (3 bytes each) fill the remainder. Raises ValueError when it cannot fit.
    """
    if size is None:
        return zstandard.ZstdCompressor(level=level, write_content_size=True, write_checksum=False).compress(data)
    if len(data) >= 1 << 32:
        raise ValueError("data too large")
    natural, blocks, last = _zstd_split(data, level)
    open_blocks = blocks[:last] + bytes([blocks[last] & 0xFE]) + blocks[last + 1:]
    for header in [natural] + _zstd_headers(len(data)):
        gap = size - len(header) - len(blocks)
        if gap == 0:
            return header + blocks
        if gap >= 3 and gap % 3 == 0:
            return header + open_blocks + ZSTD_EMPTY_BLOCK * (gap // 3 - 1) + ZSTD_LAST_EMPTY_BLOCK
    raise ValueError(f"cannot fit {len(data)} bytes into a {size}-byte zstd frame (compressed: {len(natural) + len(blocks)} bytes)")


def ezst_encode(data: bytes, magic: bytes = b"EZST", size: int | None = None, level: int = 19) -> bytes:
    """
    Wrap `data` as an EZST (encrypted) or ZSTD (plain) block. `size` = exact stored size to produce,
    for replacing an entry in place.
    """
    if magic not in (b"EZST", b"ZSTD"):
        raise ValueError("magic must be EZST or ZSTD")
    payload = zstd_frame(data, None if size is None else size - 8, level)
    if magic == b"EZST":
        n = min(len(payload) // 16 * 16, 256)
        payload = AES.new(EZST_KEY, AES.MODE_ECB).encrypt(payload[:n]) + payload[n:]
    return magic + struct.pack("<I", len(data)) + payload


def _zstd(payload: bytes, raw_size: int, verify: bool) -> bytes:
    data = zstandard.ZstdDecompressor().decompressobj().decompress(payload)
    if verify and len(data) != raw_size:
        raise ValueError(f"size mismatch {len(data)} != {raw_size}")
    return data


LUA_SIGNATURE = b"\x1bLua"
CHUNK_HEADER = LUA_SIGNATURE + b"\x54\x00\x19\x93\r\n\x1a\n" + b"\x04\x08\x08" + (0x5678).to_bytes(8, "little") + struct.pack("<d", 370.5)
HEADER_LEN = len(CHUNK_HEADER)  # 31, nupvalues follows
MAX_CHUNK = 64 << 20


def luatext_decode(blob: bytes) -> bytes | None:
    """LuaText -> Lua chunk, or None when the blob is not one."""
    if len(blob) < 8:
        return None
    size = struct.unpack_from("<I", blob, 0)[0]
    if not HEADER_LEN < size <= MAX_CHUNK:
        return None
    try:
        out = lz4.block.decompress(blob[4:], uncompressed_size=size)
    except Exception:
        return None
    return out if out[:HEADER_LEN] == CHUNK_HEADER else None


def luatext_encode(data: bytes) -> bytes:
    """LuaText stored form: u32le decoded size + raw LZ4 block."""
    block = struct.pack("<I", len(data)) + lz4.block.compress(data, store_size=False)
    if lz4.block.decompress(block[4:], uncompressed_size=len(data)) != data:
        raise ValueError("LuaText block does not decode back to its input")
    return block
