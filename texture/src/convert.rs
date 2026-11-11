//! Pixel data -> DDS (any format with a DXGI equivalent) or PNG (formats decodable here).

use crate::EPixelFormat;
use anyhow::{anyhow, bail, Result};

const DDS_FLAGS: u32 = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000;
const DDPF_FOURCC: u32 = 0x4;
const DDSCAPS_TEXTURE: u32 = 0x1000;
const D3D10_RESOURCE_DIMENSION_TEXTURE2D: u32 = 3;

/// Single-mip DDS with a DX10 header.
pub fn dds(format: EPixelFormat, width: u32, height: u32, data: &[u8]) -> Result<Vec<u8>> {
    let dxgi = format.dxgi().ok_or_else(|| anyhow!("{format:?} has no DXGI format"))?;
    let mut out = Vec::with_capacity(148 + data.len());
    out.extend_from_slice(b"DDS ");
    for v in [124, DDS_FLAGS, height, width, data.len() as u32, 0, 0] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out.extend_from_slice(&[0u8; 44]);
    for v in [32, DDPF_FOURCC] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out.extend_from_slice(b"DX10");
    out.extend_from_slice(&[0u8; 20]);
    for v in [DDSCAPS_TEXTURE, 0, 0, 0, 0] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    for v in [dxgi, D3D10_RESOURCE_DIMENSION_TEXTURE2D, 0, 1, 0] {
        out.extend_from_slice(&v.to_le_bytes());
    }
    out.extend_from_slice(data);
    Ok(out)
}

type BlockDecoder = fn(&[u8], usize, usize, &mut [u32]) -> Result<(), &'static str>;

/// RGBA8 pixels, or an error when the format cannot be decoded here.
pub fn rgba(format: EPixelFormat, width: u32, height: u32, data: &[u8]) -> Result<Vec<u8>> {
    let (w, h) = (width as usize, height as usize);
    let decoder: BlockDecoder = match format {
        EPixelFormat::R8G8B8A8 => {
            if data.len() < w * h * 4 {
                bail!("short RGBA8 data");
            }
            return Ok(data[..w * h * 4].to_vec());
        }
        EPixelFormat::L8 => {
            if data.len() < w * h {
                bail!("short L8 data");
            }
            return Ok(data[..w * h].iter().flat_map(|&l| [l, l, l, 255]).collect());
        }
        EPixelFormat::BC1 => texture2ddecoder::decode_bc1,
        EPixelFormat::BC3 => texture2ddecoder::decode_bc3,
        EPixelFormat::BC4 => texture2ddecoder::decode_bc4,
        EPixelFormat::BC5 => texture2ddecoder::decode_bc5,
        EPixelFormat::BC7 => texture2ddecoder::decode_bc7,
        other => bail!("{other:?} is not decodable to RGBA8"),
    };
    let mut pixels = vec![0u32; w * h];
    decoder(data, w, h, &mut pixels).map_err(|e| anyhow!("{format:?}: {e}"))?;
    // decoder output is 0xAARRGGBB
    Ok(pixels
        .iter()
        .flat_map(|p| [(p >> 16) as u8, (p >> 8) as u8, *p as u8, (p >> 24) as u8])
        .collect())
}

pub fn png(format: EPixelFormat, width: u32, height: u32, data: &[u8]) -> Result<Vec<u8>> {
    let pixels = rgba(format, width, height, data)?;
    let mut out = Vec::new();
    let mut enc = png::Encoder::new(&mut out, width, height);
    enc.set_color(png::ColorType::Rgba);
    enc.set_depth(png::BitDepth::Eight);
    enc.write_header()?.write_image_data(&pixels)?;
    Ok(out)
}
