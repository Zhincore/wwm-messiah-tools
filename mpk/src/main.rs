use clap::{Parser, Subcommand};
use log::LevelFilter;
use messiah_mpk::{ExtractorConfig, MPKv3Extractor};
use std::path::PathBuf;

#[derive(Parser, Debug)]
#[clap(author, version, about = "MPK Archive Extractor for Where Winds Meet", long_about = None)]
struct Args {
    #[clap(subcommand)]
    command: Commands,

    #[clap(short, long, global = true)]
    verbose: bool,
}

#[derive(Subcommand, Debug)]
enum Commands {
    /// Extract files from MPK v3 archive (Where Winds Meet)
    ExtractV3 {
        /// Path to Resources.mpkinfo file
        #[clap(value_name = "FILE")]
        mpkinfo_file: String,

        /// Output directory for extracted files
        #[clap(short, long, value_name = "DIR", default_value = "./extracted")]
        output: String,

        /// Organize extracted files by their .mpk source file (Resources.mpk, Resources1.mpk, etc.)
        #[clap(short = 'f', long)]
        organize_by_file: bool,

        /// Organize extracted files by detected content type
        #[clap(short = 't', long)]
        organize_by_type: bool,

        /// Maximum number of files to extract (0 = all)
        #[clap(short = 'm', long, default_value = "0")]
        max_files: usize,

        /// Generate a manifest file with extraction details
        #[clap(short = 'M', long)]
        manifest: bool,
    },

    /// Extract files from MPK v1 archive (legacy)
    ExtractV1 {
        /// Path to mpkinfo file
        #[clap(value_name = "FILE")]
        mpkinfo_file: String,

        /// Output directory for extracted files
        #[clap(value_name = "DIR")]
        output_dir: String,
    },

    /// Analyze MPK v3 archive structure
    AnalyzeV3 {
        /// Path to Resources.mpkinfo file
        #[clap(value_name = "FILE")]
        mpkinfo_file: String,

        /// Show detailed statistics
        #[clap(short, long)]
        detailed: bool,

        /// Sample N entries from the archive (0 = don't sample)
        #[clap(short, long, default_value = "0")]
        sample: usize,
    },
}

fn main() -> anyhow::Result<()> {
    let args = Args::parse();

    // Setup logging
    let level = if args.verbose {
        LevelFilter::Debug
    } else {
        LevelFilter::Info
    };
    simple_logger::SimpleLogger::new()
        .with_level(level)
        .init()
        .unwrap();

    match args.command {
        Commands::ExtractV3 {
            mpkinfo_file,
            output,
            organize_by_file,
            organize_by_type,
            max_files,
            manifest,
        } => {
            extract_v3(
                &mpkinfo_file,
                &output,
                organize_by_file,
                organize_by_type,
                max_files,
                manifest,
            )?;
        }

        Commands::ExtractV1 {
            mpkinfo_file,
            output_dir,
        } => {
            extract_v1(&mpkinfo_file, &output_dir)?;
        }

        Commands::AnalyzeV3 {
            mpkinfo_file,
            detailed,
            sample,
        } => {
            analyze_v3(&mpkinfo_file, detailed, sample)?;
        }
    }

    Ok(())
}

fn extract_v3(
    mpkinfo_path: &str,
    output_dir: &str,
    organize_by_file: bool,
    organize_by_type: bool,
    max_files: usize,
    generate_manifest: bool,
) -> anyhow::Result<()> {
    log::info!("=== MPK v3 Extraction ===");
    log::info!("Input: {}", mpkinfo_path);
    log::info!("Output: {}", output_dir);

    let config = ExtractorConfig {
        output_dir: PathBuf::from(output_dir),
        organize_by_file_number: organize_by_file,
        organize_by_type,
        max_files,
        verbose: true,
        generate_manifest,
    };

    let mut extractor = MPKv3Extractor::new(mpkinfo_path, config)?;

    // Show statistics
    let stats = extractor.get_stats();
    stats.print_summary();

    // Extract files
    log::info!("\nStarting extraction...");
    extractor.extract_all()?;

    if generate_manifest {
        extractor.write_manifest()?;
    }

    log::info!(
        "\nExtraction complete: {} files extracted, {} failed",
        extractor.extracted_count(),
        extractor.failed_count()
    );

    Ok(())
}

fn extract_v1(mpkinfo_path: &str, output_dir: &str) -> anyhow::Result<()> {
    log::info!("=== MPK v1 Extraction (Legacy) ===");
    log::info!("Input: {}", mpkinfo_path);
    log::info!("Output: {}", output_dir);

    use messiah_mpk::MPKFileReader;

    let mpkinfo_file = std::path::Path::new(mpkinfo_path);
    let reader = MPKFileReader::new(mpkinfo_file)?;
    reader.extract_files(output_dir)?;

    log::info!("Extraction complete");
    Ok(())
}

fn analyze_v3(mpkinfo_path: &str, detailed: bool, sample_count: usize) -> anyhow::Result<()> {
    log::info!("=== MPK v3 Analysis ===");
    log::info!("Input: {}", mpkinfo_path);

    use messiah_mpk::MPKv3Parser;
    use std::fs::File;
    use std::io::BufReader;

    let file = File::open(mpkinfo_path)?;
    let reader = BufReader::new(file);
    let parser = MPKv3Parser::parse(reader)?;

    // Print statistics
    let stats = messiah_mpk::MPKv3Stats::from_parser(&parser);
    stats.print_summary();

    if detailed {
        println!("\n=== Detailed Analysis ===");

        let (folders, files) = parser.count_folders_and_files();
        println!("Folders: {}", folders);
        println!("Files: {}", files);

        // Sample entries
        if sample_count > 0 {
            println!(
                "\n=== Entry Samples (first {}) ===",
                sample_count.min(parser.entry_count())
            );
            for (idx, entry) in parser.entries().iter().take(sample_count).enumerate() {
                println!(
                    "\nEntry {}: offset=0x{:08x} size={} file_number={} folder={}",
                    idx,
                    entry.offset,
                    entry.size,
                    entry.file_number(),
                    entry.is_folder()
                );
                println!(
                    "  file_id=0x{:016x}",
                    entry.file_id()
                );
            }
        }
    }

    Ok(())
}
