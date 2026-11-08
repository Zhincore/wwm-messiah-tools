//! MPK Version 3 Format Parser
//!
//! This module handles the new MPK v3 format used in Where Winds Meet.
//! Key differences from v1:
//! - Fixed 20-byte entries (instead of variable-length)
//! - Entries are keyed by a 64-bit file id (MurmurHash3-32 of the path in the high
//!   half, a length/tail signature of the path in the low half); no filenames
//! - `size` is the stored (compressed) size; payloads are usually EZST blocks
//!   (AES-256-ECB prefix + zstd), see tools/wwm_textures.py

use byteorder::{LittleEndian, ReadBytesExt};
use std::collections::HashMap;
use std::io::{Cursor, Read, Seek, SeekFrom};
use thiserror::Error;

#[derive(Error, Debug)]
pub enum MPKv3Error {
    #[error("Invalid MPK v3 header")]
    InvalidHeader,

    #[error("Unsupported version: {0}")]
    UnsupportedVersion(u32),

    #[error("IO error: {0}")]
    IoError(#[from] std::io::Error),

    #[error("Invalid entry structure: {0}")]
    InvalidEntry(String),

    #[error("Decompression failed: {0}")]
    DecompressionFailed(String),

    #[error("Invalid UTF-8: {0}")]
    DecodeError(#[from] std::string::FromUtf8Error),
}

/// MPK v3 file header (8 bytes)
#[derive(Debug, Clone)]
pub struct MPKv3Header {
    /// Version number (should be 3)
    pub version: u32,

    /// Total number of entries in the index
    pub entry_count: u32,
}

/// A single entry in the MPK v3 index (20 bytes fixed)
#[derive(Debug, Clone)]
pub struct MPKv3Entry {
    /// Low half of the file id: path length byte + last 3 bytes of the path (extension stripped)
    pub id_lo: u32,

    /// High half of the file id: MurmurHash3-32 of the full path
    pub id_hi: u32,

    /// Offset into the .mpk data file
    pub offset: u32,

    /// Stored size in bytes (compressed when the payload is an EZST block)
    pub size: u32,

    /// Bit 0: placeholder/folder entry (offset and size are zero)
    /// Bits 1-31: pack id (which .mpk file)
    pub flags: u32,
}

impl MPKv3Entry {
    /// 64-bit file id as used by Patch.mpkdb (there stored as signed i64)
    pub fn file_id(&self) -> u64 {
        ((self.id_hi as u64) << 32) | self.id_lo as u64
    }

    /// Extract is_folder flag from flags
    pub fn is_folder(&self) -> bool {
        (self.flags & 1) == 1
    }

    /// Extract file_number from flags (which .mpk file this entry belongs to)
    pub fn file_number(&self) -> u32 {
        self.flags >> 1
    }

    /// Get the .mpk filename for this entry
    pub fn mpk_filename(&self, basename: &str) -> String {
        let file_num = self.file_number();
        if file_num == 0 {
            format!("{}.mpk", basename)
        } else {
            format!("{}{}.mpk", basename, file_num)
        }
    }
}

/// Compression type detected from file header
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CompressionType {
    Uncompressed,
    LZ4,          // Magic: "ZZZ4"
    CompoundLZ4,  // Magic: "CCCC" (double-wrapped)
    MangledZlib,  // Magic: 0xE2 0x06
    StandardZlib, // Magic: 0x78 0x9C / 0x78 0x01 / 0x78 0xDA
}

impl CompressionType {
    /// Detect compression type from file header
    pub fn detect(data: &[u8]) -> Self {
        if data.len() < 2 {
            return CompressionType::Uncompressed;
        }

        match &data[..2.min(data.len())] {
            b"ZZ" if data.len() >= 4 && &data[..4] == b"ZZZ4" => CompressionType::LZ4,
            b"CC" if data.len() >= 4 && &data[..4] == b"CCCC" => CompressionType::CompoundLZ4,
            _ => {
                if data.len() >= 2 && data[0] == 0xE2 && data[1] == 0x06 {
                    CompressionType::MangledZlib
                } else if data[0] == 0x78 && (data[1] == 0x9C || data[1] == 0x01 || data[1] == 0xDA)
                {
                    CompressionType::StandardZlib
                } else {
                    CompressionType::Uncompressed
                }
            }
        }
    }
}

/// MPK v3 parser - reads and manages the index file
pub struct MPKv3Parser {
    header: MPKv3Header,
    entries: Vec<MPKv3Entry>,
}

impl MPKv3Parser {
    /// Parse an MPK v3 index file
    pub fn parse<R: Read + Seek>(mut reader: R) -> Result<Self, MPKv3Error> {
        // Read 8-byte header
        let mut header_bytes = [0u8; 8];
        reader.read_exact(&mut header_bytes)?;

        let mut cursor = Cursor::new(&header_bytes);
        let version = cursor.read_u32::<LittleEndian>()?;
        let entry_count = cursor.read_u32::<LittleEndian>()?;

        // Validate version
        if version != 3 {
            return Err(MPKv3Error::UnsupportedVersion(version));
        }

        log::info!(
            "MPK v3 header: version={}, entries={}",
            version,
            entry_count
        );

        let header = MPKv3Header {
            version,
            entry_count,
        };

        // Read all 20-byte entries
        let mut entries = Vec::with_capacity(entry_count as usize);
        let mut entry_bytes = [0u8; 20];

        for idx in 0..entry_count {
            reader.read_exact(&mut entry_bytes)?;
            let mut entry_cursor = Cursor::new(&entry_bytes);

            let id_lo = entry_cursor.read_u32::<LittleEndian>()?;
            let id_hi = entry_cursor.read_u32::<LittleEndian>()?;
            let offset = entry_cursor.read_u32::<LittleEndian>()?;
            let size = entry_cursor.read_u32::<LittleEndian>()?;
            let flags = entry_cursor.read_u32::<LittleEndian>()?;

            entries.push(MPKv3Entry {
                id_lo,
                id_hi,
                offset,
                size,
                flags,
            });

            if (idx + 1) % 100000 == 0 {
                log::debug!("Parsed {} entries...", idx + 1);
            }
        }

        log::info!("Successfully parsed {} entries", entries.len());

        Ok(MPKv3Parser { header, entries })
    }

    /// Get a reference to the header
    pub fn header(&self) -> &MPKv3Header {
        &self.header
    }

    /// Get all entries
    pub fn entries(&self) -> &[MPKv3Entry] {
        &self.entries
    }

    /// Get a specific entry by index
    pub fn entry(&self, idx: usize) -> Option<&MPKv3Entry> {
        self.entries.get(idx)
    }

    /// Get number of entries
    pub fn entry_count(&self) -> usize {
        self.entries.len()
    }

    /// Count entries by file_number
    pub fn entries_by_file_number(&self) -> HashMap<u32, usize> {
        let mut counts = HashMap::new();
        for entry in &self.entries {
            if !entry.is_folder() {
                *counts.entry(entry.file_number()).or_insert(0) += 1;
            }
        }
        counts
    }

    /// Count directory vs file entries
    pub fn count_folders_and_files(&self) -> (usize, usize) {
        let mut folders = 0;
        let mut files = 0;
        for entry in &self.entries {
            if entry.is_folder() {
                folders += 1;
            } else {
                files += 1;
            }
        }
        (folders, files)
    }
}

/// Decompressor for MPK v3 file data
pub struct MPKv3Decompressor;

impl MPKv3Decompressor {
    /// Decompress file data based on detected compression type
    pub fn decompress(data: &[u8]) -> Result<Vec<u8>, MPKv3Error> {
        let compression = CompressionType::detect(data);

        match compression {
            CompressionType::Uncompressed => Ok(data.to_vec()),
            CompressionType::LZ4 => Self::decompress_lz4(data),
            CompressionType::CompoundLZ4 => Self::decompress_compound_lz4(data),
            CompressionType::MangledZlib => Self::decompress_mangled_zlib(data),
            CompressionType::StandardZlib => Self::decompress_standard_zlib(data),
        }
    }

    /// Decompress LZ4 format (magic: "ZZZ4")
    fn decompress_lz4(data: &[u8]) -> Result<Vec<u8>, MPKv3Error> {
        if data.len() < 8 {
            return Err(MPKv3Error::DecompressionFailed(
                "LZ4 data too short".to_string(),
            ));
        }

        // Skip "ZZZ4" magic (4 bytes)
        // Next 4 bytes should be size, but lz4_flex handles it
        lz4_flex::decompress_size_prepended(&data[4..])
            .map_err(|e| MPKv3Error::DecompressionFailed(format!("LZ4 decompress failed: {}", e)))
    }

    /// Decompress compound LZ4 format (magic: "CCCC")
    fn decompress_compound_lz4(data: &[u8]) -> Result<Vec<u8>, MPKv3Error> {
        if data.len() < 28 {
            // 4 (CCCC) + 4 (ZZZ4) + 4 (size) + 16 (footer) minimum
            return Err(MPKv3Error::DecompressionFailed(
                "Compound LZ4 data too short".to_string(),
            ));
        }

        // Skip "CCCC" magic and verify "ZZZ4" follows
        if &data[4..8] != b"ZZZ4" {
            return Err(MPKv3Error::DecompressionFailed(
                "Invalid compound LZ4 format (missing ZZZ4)".to_string(),
            ));
        }

        // Read size at offset 8
        let mut size_cursor = Cursor::new(&data[8..12]);
        let uncompressed_size = size_cursor.read_u32::<LittleEndian>()? as usize;

        // Compressed data is from offset 12 to len-20 (20-byte footer at end)
        if data.len() < 32 {
            return Err(MPKv3Error::DecompressionFailed(
                "Not enough data for compound LZ4".to_string(),
            ));
        }

        let compressed_end = data.len() - 20;
        lz4_flex::decompress(&data[12..compressed_end], uncompressed_size)
            .map_err(|e| MPKv3Error::DecompressionFailed(format!("Compound LZ4 failed: {}", e)))
    }

    /// Decompress mangled zlib format (magic: 0xE2 0x06)
    fn decompress_mangled_zlib(data: &[u8]) -> Result<Vec<u8>, MPKv3Error> {
        if data.is_empty() {
            return Err(MPKv3Error::DecompressionFailed(
                "Mangled zlib data empty".to_string(),
            ));
        }

        // XOR obfuscation key
        const XOR_KEY: u8 = 0x9A;

        // Create a mutable copy and apply XOR to first 128 bytes
        let mut buffer = data.to_vec();
        let offset = (buffer.len() - 8) % 37;
        let end = 128.min(buffer.len() - offset);

        for i in 0..end {
            buffer[offset + i] ^= XOR_KEY;
        }

        // Remove trailing 8 bytes if present
        let decode_end = if buffer.len() > 8 {
            buffer.len() - 8
        } else {
            buffer.len()
        };

        // Decompress with zlib
        let cursor = Cursor::new(&buffer[..decode_end]);
        let mut decoder = compress::zlib::Decoder::new(cursor);
        let mut result = Vec::new();
        decoder
            .read_to_end(&mut result)
            .map_err(|e| MPKv3Error::DecompressionFailed(format!("zlib decode failed: {}", e)))?;

        Ok(result)
    }

    /// Decompress standard zlib format (magic: 0x78 0x9C / 0x78 0x01 / 0x78 0xDA)
    fn decompress_standard_zlib(data: &[u8]) -> Result<Vec<u8>, MPKv3Error> {
        let cursor = Cursor::new(data);
        let mut decoder = compress::zlib::Decoder::new(cursor);
        let mut result = Vec::new();
        decoder
            .read_to_end(&mut result)
            .map_err(|e| MPKv3Error::DecompressionFailed(format!("zlib decode failed: {}", e)))?;
        Ok(result)
    }
}

/// Statistics about the MPK archive
#[derive(Debug, Default)]
pub struct MPKv3Stats {
    pub total_entries: usize,
    pub total_files: usize,
    pub total_folders: usize,
    pub total_uncompressed_size: u64,
    pub entries_by_file: HashMap<u32, usize>,
}

impl MPKv3Stats {
    /// Calculate statistics from parser
    pub fn from_parser(parser: &MPKv3Parser) -> Self {
        let (folders, files) = parser.count_folders_and_files();
        let entries_by_file = parser.entries_by_file_number();

        let total_uncompressed_size: u64 = parser.entries().iter().map(|e| e.size as u64).sum();

        MPKv3Stats {
            total_entries: parser.entry_count(),
            total_files: files,
            total_folders: folders,
            total_uncompressed_size,
            entries_by_file,
        }
    }

    /// Print human-readable statistics
    pub fn print_summary(&self) {
        println!("\n=== MPK v3 Statistics ===");
        println!("Total Entries:          {}", self.total_entries);
        println!("Files:                  {}", self.total_files);
        println!("Folders:                {}", self.total_folders);
        println!(
            "Total Uncompressed:     {} bytes ({:.2} GB)",
            self.total_uncompressed_size,
            self.total_uncompressed_size as f64 / 1_000_000_000.0
        );
        println!("\nEntries by .mpk file:");
        let mut file_nums: Vec<_> = self.entries_by_file.keys().collect();
        file_nums.sort();
        for file_num in file_nums {
            let count = self.entries_by_file[file_num];
            let mpk_name = if *file_num == 0 {
                "Resources.mpk".to_string()
            } else {
                format!("Resources{}.mpk", file_num)
            };
            println!("  {} ({} files)", mpk_name, count);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_compression_type_detection() {
        let lz4_data = b"ZZZ4....";
        assert_eq!(CompressionType::detect(lz4_data), CompressionType::LZ4);

        let compound_data = b"CCCC....";
        assert_eq!(
            CompressionType::detect(compound_data),
            CompressionType::CompoundLZ4
        );

        let mangled_data = &[0xE2u8, 0x06, 0xFF, 0xFF];
        assert_eq!(
            CompressionType::detect(mangled_data),
            CompressionType::MangledZlib
        );

        let zlib_data = &[0x78u8, 0x9C, 0xFF, 0xFF];
        assert_eq!(
            CompressionType::detect(zlib_data),
            CompressionType::StandardZlib
        );

        let uncompressed = &[0xFFu8, 0xFF, 0xFF, 0xFF];
        assert_eq!(
            CompressionType::detect(uncompressed),
            CompressionType::Uncompressed
        );
    }

    #[test]
    fn test_entry_flag_extraction() {
        let entry = MPKv3Entry {
            id_lo: 0,
            id_hi: 0,
            offset: 0,
            size: 0,
            flags: 0x00000000,
        };
        assert!(!entry.is_folder());
        assert_eq!(entry.file_number(), 0);

        let entry_folder = MPKv3Entry {
            id_lo: 0,
            id_hi: 0,
            offset: 0,
            size: 0,
            flags: 0x00000001,
        };
        assert!(entry_folder.is_folder());
        assert_eq!(entry_folder.file_number(), 0);

        let entry_file1 = MPKv3Entry {
            id_lo: 0,
            id_hi: 0,
            offset: 0,
            size: 0,
            flags: 0x00000002,
        };
        assert!(!entry_file1.is_folder());
        assert_eq!(entry_file1.file_number(), 1);

        let entry_file74_folder = MPKv3Entry {
            id_lo: 0,
            id_hi: 0,
            offset: 0,
            size: 0,
            flags: 0x00000097, // (75 << 1) | 1
        };
        assert!(entry_file74_folder.is_folder());
        assert_eq!(entry_file74_folder.file_number(), 75);
    }

    #[test]
    fn test_mpk_filename_generation() {
        let entry0 = MPKv3Entry {
            id_lo: 0,
            id_hi: 0,
            offset: 0,
            size: 0,
            flags: 0,
        };
        assert_eq!(entry0.mpk_filename("Resources"), "Resources.mpk");

        let entry1 = MPKv3Entry {
            id_lo: 0,
            id_hi: 0,
            offset: 0,
            size: 0,
            flags: 2,
        };
        assert_eq!(entry1.mpk_filename("Resources"), "Resources1.mpk");

        let entry42 = MPKv3Entry {
            id_lo: 0,
            id_hi: 0,
            offset: 0,
            size: 0,
            flags: 84, // 42 << 1
        };
        assert_eq!(entry42.mpk_filename("Resources"), "Resources42.mpk");
    }
}
