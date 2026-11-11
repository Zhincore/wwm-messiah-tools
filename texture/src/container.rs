//! Texture container (`02 02 02 01`):
//! ```text
//! 0x00  magic
//! 0x05  u8 pixel_format, u8 mip_count
//! 0x0c  u16 width, u16 height
//! 0x28  mip records, smallest first:
//!         u32 blob_size, u16 w, u16 h, u16 depth, u16 pitch, u32 raw_size
//!         EZST block, payload length = blob_size - 24
//! end   16-byte trailer
//! ```

use crate::{ezst, EPixelFormat};
use anyhow::{bail, Result};

pub const MAGIC: [u8; 4] = [0x02, 0x02, 0x02, 0x01];
const HEADER: usize = 0x28;
const MIP_RECORD: usize = 16;
const TRAILER: usize = 16;

#[derive(Debug)]
pub struct Mip {
    pub width: u16,
    pub height: u16,
    pub depth: u16,
    pub pitch: u16,
    /// Decoded pixel data; empty when the mip has no local payload (streamed asset).
    pub data: Vec<u8>,
}

#[derive(Debug)]
pub struct Texture {
    pub format: Option<EPixelFormat>,
    pub format_id: u8,
    pub mip_count: u8,
    pub width: u16,
    pub height: u16,
    pub mips: Vec<Mip>,
    pub warnings: Vec<String>,
}

impl Texture {
    /// Largest mip that carries pixel data.
    pub fn top_mip(&self) -> Option<&Mip> {
        self.mips
            .iter()
            .filter(|m| !m.data.is_empty())
            .max_by_key(|m| m.width as u32 * m.height as u32)
    }
}

fn u16_at(buf: &[u8], pos: usize) -> u16 {
    u16::from_le_bytes([buf[pos], buf[pos + 1]])
}

fn u32_at(buf: &[u8], pos: usize) -> u32 {
    u32::from_le_bytes([buf[pos], buf[pos + 1], buf[pos + 2], buf[pos + 3]])
}

pub fn parse(buf: &[u8]) -> Result<Texture> {
    if buf.len() < HEADER || buf[..4] != MAGIC {
        bail!("not a texture container");
    }
    let mut tex = Texture {
        format: EPixelFormat::try_from(buf[5]).ok(),
        format_id: buf[5],
        mip_count: buf[6],
        width: u16_at(buf, 0x0c),
        height: u16_at(buf, 0x0e),
        mips: Vec::new(),
        warnings: Vec::new(),
    };
    let mut pos = HEADER;
    while pos + MIP_RECORD <= buf.len() {
        let blob = u32_at(buf, pos) as usize;
        let (width, height) = (u16_at(buf, pos + 4), u16_at(buf, pos + 6));
        let (depth, pitch) = (u16_at(buf, pos + 8), u16_at(buf, pos + 10));
        let raw = u32_at(buf, pos + 12) as usize;
        pos += MIP_RECORD;
        let end = pos + 8 + blob.saturating_sub(24);
        if end > buf.len() || !ezst::is_block(&buf[pos..end]) {
            tex.warnings.push(format!("no EZST block at {pos}"));
            break;
        }
        let data = if raw == 0 {
            Vec::new()
        } else {
            match ezst::decode(&buf[pos..end]) {
                Ok(d) => {
                    if d.len() != raw {
                        tex.warnings.push(format!("mip {width}x{height}: got {} want {raw}", d.len()));
                    }
                    d
                }
                Err(e) => {
                    tex.warnings.push(format!("mip {width}x{height}: {e}"));
                    Vec::new()
                }
            }
        };
        tex.mips.push(Mip { width, height, depth, pitch, data });
        pos = end;
        if pos + TRAILER == buf.len() {
            pos = buf.len();
            break;
        }
    }
    if pos != buf.len() {
        tex.warnings.push(format!("ended at {pos}/{}", buf.len()));
    }
    Ok(tex)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `ZSTD` block around a single raw zstd block holding `data`.
    fn plain_block(data: &[u8]) -> Vec<u8> {
        let mut b = b"ZSTD".to_vec();
        b.extend_from_slice(&(data.len() as u32).to_le_bytes());
        b.extend_from_slice(&[0x28, 0xb5, 0x2f, 0xfd, 0x20, data.len() as u8]);
        let header = ((data.len() as u32) << 3) | 1;
        b.extend_from_slice(&header.to_le_bytes()[..3]);
        b.extend_from_slice(data);
        b
    }

    #[test]
    fn parses_single_mip_container() {
        let pixels: Vec<u8> = (0..16).collect();
        let block = plain_block(&pixels);
        let mut buf = vec![0u8; HEADER];
        buf[..4].copy_from_slice(&MAGIC);
        buf[5] = EPixelFormat::R8G8B8A8 as u8;
        buf[6] = 1;
        buf[0x0c..0x0e].copy_from_slice(&2u16.to_le_bytes());
        buf[0x0e..0x10].copy_from_slice(&2u16.to_le_bytes());
        buf.extend_from_slice(&((block.len() - 8 + 24) as u32).to_le_bytes());
        for v in [2u16, 2, 1, 8] {
            buf.extend_from_slice(&v.to_le_bytes());
        }
        buf.extend_from_slice(&(pixels.len() as u32).to_le_bytes());
        buf.extend_from_slice(&block);
        buf.extend_from_slice(&[0u8; TRAILER]);

        let tex = parse(&buf).unwrap();
        assert!(tex.warnings.is_empty(), "{:?}", tex.warnings);
        assert_eq!(tex.format, Some(EPixelFormat::R8G8B8A8));
        assert_eq!((tex.width, tex.height), (2, 2));
        let mip = tex.top_mip().unwrap();
        assert_eq!(mip.data, pixels);
        assert_eq!(crate::convert::rgba(EPixelFormat::R8G8B8A8, 2, 2, &mip.data).unwrap(), pixels);
        assert_eq!(crate::convert::dds(EPixelFormat::R8G8B8A8, 2, 2, &mip.data).unwrap().len(), 148 + 16);
    }

    #[test]
    fn rejects_other_data() {
        assert!(parse(b"not a texture, definitely not one, no way at all").is_err());
    }
}
