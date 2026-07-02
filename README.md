# Bulk-Compressor

Bulk-Compressor is a high-performance, multithreaded media compression engine designed for efficiently reducing storage consumption across large datasets while maintaining data integrity. It supports automatic compression of images, videos, audio files, and PDF documents, performs duplicate detection using SHA-256 hashing, maintains resumable checkpoints, and includes a comprehensive post-processing verification pipeline capable of detecting corrupted outputs and restoring quarantined files.

The project is intended for archival systems, media libraries, NAS devices, backup optimization, enterprise storage reduction, and large-scale dataset preprocessing.

---

## Features

### Compression Engine

- Parallel processing using ThreadPoolExecutor
- Separate worker pools for CPU-intensive video encoding and lightweight media compression
- Automatic recursive directory traversal
- SHA-256 duplicate detection
- Resume interrupted compression jobs
- Automatic temporary file management
- Metadata preservation
- Loss-aware replacement strategy
- Automatic overwrite only if output is smaller
- Progress monitoring
- Detailed compression statistics

---

### Supported Formats

#### Images

- JPG
- JPEG
- PNG
- WEBP
- BMP
- TIFF
- TIF

Animated GIFs are intentionally skipped to prevent animation corruption.

Compression includes:

- Progressive JPEG encoding
- PNG maximum compression
- WEBP optimization
- TIFF LZW compression
- BMP → PNG conversion

---

#### Videos

Supported formats include:

- MP4
- MKV
- AVI
- MOV
- WMV
- WEBM
- FLV
- M4V
- TS
- 3GP

Video compression uses:

- H.264 (libx264)
- CRF-based quality encoding
- AAC audio encoding
- FastStart optimization
- Metadata preservation

Videos between one and three seconds are automatically identified and deleted.

---

#### Audio

Supported formats:

- MP3
- WAV
- FLAC
- M4A
- AAC
- AIFF
- OGG
- OPUS
- WMA
- AMR

Features include:

- AAC transcoding
- Adaptive bitrate selection
- Speech-aware compression
- Metadata preservation
- Automatic conversion to M4A

---

#### Documents

Currently supported:

- PDF

Compression uses Ghostscript with duplicate image detection and font compression.

---

## Duplicate Detection

Before compression begins, every file is hashed using SHA-256.

If two files are byte-identical:

- Only one copy is processed
- Remaining copies are skipped
- Duplicate files are reported

This significantly reduces unnecessary processing on backup collections.

---

## Resume Support

Interrupted compression sessions can be resumed automatically.

Every successfully processed file is written into a checkpoint log.

Restarting the compressor skips files already completed.

No manual intervention is required.

---

## Verification Pipeline

The repository includes an independent verification engine capable of validating every generated output.

Verification includes:

### Images

- Complete decode
- Dimension validation
- Pixel loading

### Videos

- FFprobe inspection
- Full FFmpeg decode
- Resolution validation
- Stream validation

### Audio

- FFprobe analysis
- Full decode validation
- Audio stream verification

### PDFs

- PikePDF validation
- Ghostscript rendering verification

Files failing verification are automatically quarantined.

---

## Automatic Quarantine

Corrupted files are moved into:

```
_Corrupted_Files/
```

A detailed report containing every corrupted file and failure reason is generated automatically.

---

## Restoration Utility

The included restoration tool restores quarantined files back to their original locations using the generated report.

It supports:

- Original directory recreation
- Collision handling
- Batch restoration
- Automatic cleanup after successful restore

---

# Repository Structure

```
bulk-compressor/

├── engine_compress.py
├── verify_backup.py
├── restore_files.py
└── README.md
```

---

# Requirements

## Python

Python 3.10+

---

## External Software

### FFmpeg

Required for:

- Video compression
- Audio compression
- Video verification
- Audio verification

Linux

```bash
sudo apt install ffmpeg
```

macOS

```bash
brew install ffmpeg
```

Windows

Download FFmpeg and add it to the system PATH.

---

### Ghostscript

Required for PDF compression and PDF verification.

Linux

```bash
sudo apt install ghostscript
```

macOS

```bash
brew install ghostscript
```

Windows

Install Ghostscript and add the executable to PATH.

---

## Python Dependencies

```bash
pip install pillow
```

Optional:

```bash
pip install pikepdf
```

---

# Installation

Clone the repository

```bash
git clone https://github.com/<username>/bulk-compressor.git

cd bulk-compressor
```

Install dependencies

```bash
pip install pillow

pip install pikepdf
```

Install FFmpeg

Verify installation

```bash
ffmpeg -version
```

Install Ghostscript

Verify installation

```bash
gs --version
```

Windows

```bash
gswin64c.exe -version
```

---

# Usage

## Compress an Entire Directory

```bash
python engine_compress.py "/path/to/directory"
```

Example

```bash
python engine_compress.py "D:\Media"
```

---

## Verify All Files

```bash
python verify_backup.py "/path/to/directory"
```

Example

```bash
python verify_backup.py "D:\Media"
```

---

## Restore Quarantined Files

```bash
python restore_files.py "/path/to/directory"
```

Example

```bash
python restore_files.py "D:\Media"
```

---

# Compression Workflow

```
Directory Scan
        │
        ▼
Duplicate Detection
        │
        ▼
Checkpoint Resume
        │
        ▼
Parallel Compression
        │
        ▼
Replace If Smaller
        │
        ▼
Verification
        │
        ▼
Quarantine Corrupted Files
        │
        ▼
Optional Restoration
```

---

# Output

The compressor reports:

- Total files processed
- Files compressed
- Files skipped
- Duplicate files
- Deleted short videos
- Errors encountered
- Total storage saved

Example

```
================================================================================

COMPRESSION COMPLETE

================================================================================

Files processed : 18352

Files compressed : 16127

Files skipped : 2018

Videos deleted : 76

Errors : 14

Total space saved : 642.38 GB

================================================================================
```

---

# Performance

Bulk-Compressor uses two independent worker pools.

Fast worker pool

- Images
- Audio
- PDFs

Video worker pool

- Dedicated FFmpeg encoding threads

Video encoding internally utilizes all available CPU cores while maintaining controlled parallelism to prevent system oversubscription.

---

# Safety Guarantees

Bulk-Compressor never overwrites a file unless:

- Compression completed successfully
- Output file is valid
- Output size is smaller
- Temporary file creation succeeded

Original files remain untouched whenever compression is unsuccessful.

---

# Known Limitations

- Microsoft Office documents are currently skipped.
- Animated GIF compression is intentionally disabled.
- FFmpeg and Ghostscript must be available in the system PATH.
- Video compression currently targets H.264.
- GPU acceleration is not currently implemented.

---

# Future Improvements

- HEVC encoding
- AV1 encoding
- GPU acceleration
- Office document optimization
- ZIP archive optimization
- RAW image support
- Parallel directory scheduling
- Configurable compression profiles
- JSON reporting
- Benchmark mode
- CLI configuration system
- Containerized deployment
- Automatic hardware detection

---

# License

This project is released under the MIT License.
