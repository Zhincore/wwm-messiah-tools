//! EZST blocks: `"EZST" u32 raw_size payload`, where the first `min(floor16(len), 256)` payload
//! bytes are AES-256-ECB encrypted and the decrypted payload is one zstd frame.
//! `"ZSTD"` uses the same header without encryption.

use aes::cipher::{generic_array::GenericArray, BlockDecrypt, KeyInit};
use aes::Aes256;
use anyhow::{anyhow, bail, Result};
use std::io::Read;

/// Fixed key, from ArArgon/yysls-re (`messiah.codec.ezst`).
const KEY_WORDS: [u64; 4] = [
    0x556A586E32723575,
    0x38782F913F152867,
    0x2D3B615062536756,
    0x6B59703373367639,
];
const MAX_ENCRYPTED: usize = 256;

fn key() -> [u8; 32] {
    let mut k = [0u8; 32];
    for (i, w) in KEY_WORDS.iter().enumerate() {
        k[i * 8..i * 8 + 8].copy_from_slice(&w.to_le_bytes());
    }
    k
}

pub fn is_block(buf: &[u8]) -> bool {
    buf.len() >= 8 && (&buf[..4] == b"EZST" || &buf[..4] == b"ZSTD")
}

fn unzstd(payload: &[u8]) -> Result<Vec<u8>> {
    let mut src = payload;
    let mut dec = ruzstd::StreamingDecoder::new(&mut src).map_err(|e| anyhow!("zstd: {e}"))?;
    let mut out = Vec::new();
    dec.read_to_end(&mut out)?;
    Ok(out)
}

/// Decode one block. The encrypted prefix depends on the true payload length; when the slice
/// carries trailing bytes the length is unknown, so shorter prefixes are tried in turn.
pub fn decode(block: &[u8]) -> Result<Vec<u8>> {
    if !is_block(block) {
        bail!("not an EZST block");
    }
    let payload = &block[8..];
    if &block[..4] == b"ZSTD" {
        return unzstd(payload);
    }
    let cipher = Aes256::new(GenericArray::from_slice(&key()));
    let mut n = (payload.len() / 16 * 16).min(MAX_ENCRYPTED);
    let mut last = anyhow!("empty payload");
    while n > 0 {
        let mut buf = payload.to_vec();
        for chunk in buf[..n].chunks_exact_mut(16) {
            cipher.decrypt_block(GenericArray::from_mut_slice(chunk));
        }
        match unzstd(&buf) {
            Ok(out) => return Ok(out),
            Err(e) => last = e,
        }
        n -= 16;
    }
    Err(last.context("EZST decode failed"))
}

/// Declared decompressed size of a block.
pub fn raw_size(block: &[u8]) -> u32 {
    u32::from_le_bytes([block[4], block[5], block[6], block[7]])
}
