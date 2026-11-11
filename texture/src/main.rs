use anyhow::{Context, Result};
use clap::Parser;
use messiah_texture::{container, convert, ezst};
use std::fs;
use std::path::{Path, PathBuf};

/// Convert extracted texture payloads (raw `02 02 02 01` containers or EZST-wrapped) to PNG/DDS.
#[derive(Parser)]
struct Args {
    /// Payload files or directories of payload files
    inputs: Vec<PathBuf>,
    /// Output directory
    #[clap(short, long, default_value = ".")]
    output: PathBuf,
    /// Always write DDS instead of PNG
    #[clap(long)]
    dds: bool,
}

fn convert_file(path: &Path, args: &Args) -> Result<Option<PathBuf>> {
    let mut buf = fs::read(path)?;
    if ezst::is_block(&buf) {
        buf = ezst::decode(&buf)?;
    }
    if buf.len() < 4 || buf[..4] != container::MAGIC {
        return Ok(None);
    }
    let tex = container::parse(&buf)?;
    for w in &tex.warnings {
        eprintln!("{}: {w}", path.display());
    }
    let (Some(format), Some(mip)) = (tex.format, tex.top_mip()) else {
        return Ok(None);
    };
    let (w, h) = (mip.width as u32, mip.height as u32);
    let stem = path.file_stem().unwrap_or_default().to_string_lossy();
    let png = if args.dds { None } else { convert::png(format, w, h, &mip.data).ok() };
    let (ext, bytes) = match png {
        Some(bytes) => ("png", bytes),
        None => ("dds", convert::dds(format, w, h, &mip.data)?),
    };
    let out = args.output.join(format!("{stem}.{ext}"));
    fs::write(&out, bytes)?;
    Ok(Some(out))
}

fn main() -> Result<()> {
    let args = Args::parse();
    fs::create_dir_all(&args.output)?;
    let mut files = Vec::new();
    for input in &args.inputs {
        if input.is_dir() {
            for e in fs::read_dir(input)? {
                let p = e?.path();
                if p.is_file() {
                    files.push(p);
                }
            }
        } else {
            files.push(input.clone());
        }
    }
    let (mut done, mut skipped) = (0, 0);
    for f in &files {
        match convert_file(f, &args).with_context(|| f.display().to_string()) {
            Ok(Some(_)) => done += 1,
            Ok(None) => skipped += 1,
            Err(e) => eprintln!("{e:#}"),
        }
    }
    println!("{done} converted, {skipped} skipped, {} failed", files.len() - done - skipped);
    Ok(())
}
