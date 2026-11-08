//! MPK v3 Extractor
//!
//! Extracts files from MPK v3 archives to disk.
//! Handles:
//! - Reading index from .mpkinfo file
//! - Locating file data in .mpk files by file_number
//! - Decompressing per-file based on magic bytes
//! - Writing extracted files with placeholder names (by index or hash)

use crate::v3_parser::{CompressionType, MPKv3Decompressor, MPKv3Entry, MPKv3Parser, MPKv3Stats};
use anyhow::{anyhow, Context};
use indicatif::ProgressBar;
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{BufReader, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

/// Configuration for extraction
#[derive(Debug, Clone)]
pub struct ExtractorConfig {
    /// Output directory for extracted files
    pub output_dir: PathBuf,

    /// Whether to create subdirectories by file_number
    pub organize_by_file_number: bool,

    /// Whether to create subdirectories by content type (detected from magic)
    pub organize_by_type: bool,

    /// Maximum number of files to extract (0 = all)
    pub max_files: usize,

    /// Whether to show detailed progress
    pub verbose: bool,

    /// Whether to generate a manifest file
    pub generate_manifest: bool,
}

impl Default for ExtractorConfig {
    fn default() -> Self {
        ExtractorConfig {
            output_dir: PathBuf::from("./extracted"),
            organize_by_file_number: true,
            organize_by_type: false,
            max_files: 0,
            verbose: false,
            generate_manifest: false,
        }
    }
}

/// Information about an extracted file
#[derive(Debug, Clone)]
pub struct ExtractedFileInfo {
    /// Index in the archive
    pub index: usize,

    /// id_lo value from entry (likely hash or type)
    pub id_lo: u32,

    /// id_hi value from entry
    pub id_hi: u32,

    /// Original offset in .mpk
    pub offset: u32,

    /// Original size in .mpk (possibly compressed)
    pub compressed_size: u32,

    /// Actual uncompressed size
    pub uncompressed_size: u32,

    /// Which .mpk file it came from
    pub file_number: u32,

    /// Detected compression type
    pub compression_type: CompressionType,

    /// Output path where it was extracted
    pub output_path: PathBuf,

    /// Whether extraction was successful
    pub success: bool,

    /// Error message if extraction failed
    pub error: Option<String>,
}

/// Main extractor
pub struct MPKv3Extractor {
    parser: MPKv3Parser,
    config: ExtractorConfig,
    mpk_files: HashMap<u32, File>,
    extracted_files: Vec<ExtractedFileInfo>,
}

impl MPKv3Extractor {
    /// Create a new extractor from an mpkinfo file
    pub fn new<P: AsRef<Path>>(mpkinfo_path: P, config: ExtractorConfig) -> anyhow::Result<Self> {
        let mpkinfo_file = File::open(&mpkinfo_path).context("Failed to open .mpkinfo file")?;
        let reader = BufReader::new(mpkinfo_file);

        let parser = MPKv3Parser::parse(reader).context("Failed to parse MPK v3 index")?;

        log::info!("Loaded MPK v3 index with {} entries", parser.entry_count());

        // Create output directory
        fs::create_dir_all(&config.output_dir).context("Failed to create output directory")?;

        let mpk_files = HashMap::new();
        let extracted_files = Vec::new();

        Ok(MPKv3Extractor {
            parser,
            config,
            mpk_files,
            extracted_files,
        })
    }

    /// Get or open an .mpk data file
    fn get_mpk_file(&mut self, file_number: u32, base_dir: &Path) -> anyhow::Result<&mut File> {
        if self.mpk_files.contains_key(&file_number) {
            return Ok(self.mpk_files.get_mut(&file_number).unwrap());
        }

        // Construct the .mpk filename
        let mpk_filename = if file_number == 0 {
            "Resources.mpk".to_string()
        } else {
            format!("Resources{}.mpk", file_number)
        };

        let mpk_path = base_dir.join(&mpk_filename);
        let file =
            File::open(&mpk_path).context(format!("Failed to open {}", mpk_path.display()))?;

        self.mpk_files.insert(file_number, file);
        Ok(self.mpk_files.get_mut(&file_number).unwrap())
    }

    /// Extract all files from the archive
    pub fn extract_all(&mut self) -> anyhow::Result<()> {
        let entries = self.parser.entries().to_vec();
        let total = if self.config.max_files == 0 {
            entries.len()
        } else {
            self.config.max_files.min(entries.len())
        };

        let bar = ProgressBar::new(total as u64);
        bar.set_style(
            indicatif::ProgressStyle::default_bar()
                .template("[{bar:40.cyan/blue}] {pos}/{len} ({eta})")
                .progress_chars("#>-"),
        );

        let mpkinfo_dir = std::env::current_dir()?;

        for (idx, entry) in entries.iter().enumerate() {
            if self.config.max_files > 0 && idx >= self.config.max_files {
                break;
            }

            let result = self.extract_single_file(idx, entry, &mpkinfo_dir);

            match result {
                Ok(info) => {
                    self.extracted_files.push(info);
                }
                Err(e) => {
                    let info = ExtractedFileInfo {
                        index: idx,
                        id_lo: entry.id_lo,
                        id_hi: entry.id_hi,
                        offset: entry.offset,
                        compressed_size: entry.size,
                        uncompressed_size: 0,
                        file_number: entry.file_number(),
                        compression_type: CompressionType::Uncompressed,
                        output_path: PathBuf::new(),
                        success: false,
                        error: Some(e.to_string()),
                    };
                    self.extracted_files.push(info);

                    if self.config.verbose {
                        log::warn!("Failed to extract entry {}: {}", idx, e);
                    }
                }
            }

            bar.inc(1);
        }

        bar.finish();
        log::info!(
            "Extraction complete: {} files extracted successfully",
            self.extracted_files.iter().filter(|f| f.success).count()
        );

        Ok(())
    }

    /// Extract a single file entry
    fn extract_single_file(
        &mut self,
        index: usize,
        entry: &MPKv3Entry,
        base_dir: &Path,
    ) -> anyhow::Result<ExtractedFileInfo> {
        // Skip folders
        if entry.is_folder() {
            let output_path = self.get_output_path(index, entry);
            fs::create_dir_all(&output_path)?;
            return Ok(ExtractedFileInfo {
                index,
                id_lo: entry.id_lo,
                id_hi: entry.id_hi,
                offset: entry.offset,
                compressed_size: 0,
                uncompressed_size: 0,
                file_number: entry.file_number(),
                compression_type: CompressionType::Uncompressed,
                output_path,
                success: true,
                error: None,
            });
        }

        // Read file data from .mpk
        let mpk_file = self.get_mpk_file(entry.file_number(), base_dir)?;
        mpk_file.seek(SeekFrom::Start(entry.offset as u64))?;

        let mut compressed_data = vec![0u8; entry.size as usize];
        mpk_file.read_exact(&mut compressed_data)?;

        // Detect and decompress
        let compression_type = CompressionType::detect(&compressed_data);
        let decompressed = MPKv3Decompressor::decompress(&compressed_data)?;

        // Determine output path
        let output_path = self.get_output_path(index, entry);

        // Create parent directories
        if let Some(parent) = output_path.parent() {
            fs::create_dir_all(parent)?;
        }

        // Write file
        let mut output_file = File::create(&output_path)?;
        output_file.write_all(&decompressed)?;

        Ok(ExtractedFileInfo {
            index,
            id_lo: entry.id_lo,
            id_hi: entry.id_hi,
            offset: entry.offset,
            compressed_size: entry.size,
            uncompressed_size: decompressed.len() as u32,
            file_number: entry.file_number(),
            compression_type,
            output_path,
            success: true,
            error: None,
        })
    }

    /// Get output path for a file
    fn get_output_path(&self, index: usize, entry: &MPKv3Entry) -> PathBuf {
        let mut path = self.config.output_dir.clone();

        if self.config.organize_by_file_number {
            let file_dir = if entry.file_number() == 0 {
                "Resources".to_string()
            } else {
                format!("Resources{}", entry.file_number())
            };
            path.push(file_dir);
        }

        if self.config.organize_by_type {
            let type_dir = self.get_type_dir(&entry);
            path.push(type_dir);
        }

        // Use a placeholder filename with index
        let filename = format!(
            "{:08x}_{:08x}_{:08x}.bin",
            entry.id_lo, entry.id_hi, index
        );
        path.push(filename);

        path
    }

    /// Determine content type from magic bytes or id_lo
    fn get_type_dir(&self, _entry: &MPKv3Entry) -> String {
        // This could be enhanced to detect file types
        "files".to_string()
    }

    /// Generate a manifest file with extraction information
    pub fn write_manifest(&self) -> anyhow::Result<()> {
        let manifest_path = self.config.output_dir.join("MANIFEST.txt");
        let mut file = File::create(&manifest_path)?;

        writeln!(file, "MPK v3 Extraction Manifest")?;
        writeln!(file, "==========================\n")?;

        writeln!(file, "Total Entries: {}", self.extracted_files.len())?;
        writeln!(
            file,
            "Successfully Extracted: {}",
            self.extracted_files.iter().filter(|f| f.success).count()
        )?;
        writeln!(
            file,
            "Failed: {}",
            self.extracted_files.iter().filter(|f| !f.success).count()
        )?;

        writeln!(file, "\nExtracted Files:\n")?;
        for info in &self.extracted_files {
            if info.success {
                writeln!(
                    file,
                    "Index: {}\n  id_lo: 0x{:08x}\n  id_hi: 0x{:08x}\n  file_number: {}\n  compression: {:?}\n  size: {} -> {}\n  output: {}\n",
                    info.index,
                    info.id_lo,
                    info.id_hi,
                    info.file_number,
                    info.compression_type,
                    info.compressed_size,
                    info.uncompressed_size,
                    info.output_path.display()
                )?;
            } else {
                writeln!(
                    file,
                    "Index: {}\n  ERROR: {}\n",
                    info.index,
                    info.error.as_deref().unwrap_or("Unknown")
                )?;
            }
        }

        log::info!("Manifest written to {}", manifest_path.display());
        Ok(())
    }

    /// Get statistics about the archive
    pub fn get_stats(&self) -> MPKv3Stats {
        MPKv3Stats::from_parser(&self.parser)
    }

    /// Get extracted file count
    pub fn extracted_count(&self) -> usize {
        self.extracted_files.iter().filter(|f| f.success).count()
    }

    /// Get failed extraction count
    pub fn failed_count(&self) -> usize {
        self.extracted_files.iter().filter(|f| !f.success).count()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extractor_config_default() {
        let config = ExtractorConfig::default();
        assert_eq!(config.output_dir, PathBuf::from("./extracted"));
        assert!(config.organize_by_file_number);
        assert!(!config.organize_by_type);
        assert_eq!(config.max_files, 0);
    }
}
