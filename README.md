# Bulk Compressor

**Bulk Compressor** is a local, recursive, multi-threaded file optimization and integrity-verification toolkit for large media collections.

It is designed around a deliberately conservative principle:

> **Never replace a file with a larger compressed result.**

The project scans a directory tree, detects byte-identical duplicates, compresses supported media with format-specific engines, verifies the resulting collection, quarantines files that fail decoding, and provides a restoration utility for quarantined files.

It is intentionally **local-first**: files are processed on the machine where the scripts are executed. There is no upload service, cloud dependency, database, or proprietary compression backend.

---

## What This Repository Actually Does

The repository contains three cooperating command-line utilities:

| Script | Responsibility |
|---|---|
| `engine_compress.py` | Recursive duplicate detection + bulk compression/optimization |
| `verify_baaaaackup.py` | Full media integrity verification + corruption quarantine |
| `restore_files.py` | Restore quarantined files to their original paths |

The intended workflow is:

```text
SOURCE DIRECTORY
       │
       ▼
┌──────────────────────────┐
│ engine_compress.py       │
│                          │
│ • recursive scan         │
│ • duplicate detection    │
│ • image optimization     │
│ • video transcoding      │
│ • audio transcoding      │
│ • PDF optimization       │
│ • checkpoint/resume      │
│ • only keep smaller file │
└────────────┬─────────────┘
             │
             ▼
       compressed data
             │
             ▼
┌──────────────────────────┐
│ verify_baaaaackup.py     │
│                          │
│ • image decode           │
│ • FFprobe inspection     │
│ • full video decode      │
│ • full audio decode      │
│ • PDF structural/render  │
│ • quarantine failures    │
└────────────┬─────────────┘
             │
       ┌─────┴─────┐
       ▼           ▼
     VALID       INVALID
                   │
                   ▼
          _Corrupted_Files/
                   │
                   ▼
          corrupt_files_report.txt
                   │
                   ▼
┌──────────────────────────┐
│ restore_files.py         │
│                          │
│ • reads restoration log  │
│ • finds quarantined data │
│ • recreates directories  │
│ • moves files back       │
└──────────────────────────┘
```

---

## Core Design Philosophy

This is not a "compress everything at any cost" utility.

The compressor uses a **replace-if-smaller** policy for ordinary conversions:

```text
original_size = S
compressed_size = C

if C < S:
    replace original
else:
    discard compressed output
```

This prevents a common batch-compression failure mode where already-optimized files become larger after transcoding.

There are, however, two intentionally destructive operations:

1. **1–3 second videos are deleted.**
2. **Files that fail verification are moved into `_Corrupted_Files/`.**

Read the safety section before running this on irreplaceable data.

---

# Features

## 1. Recursive bulk processing

The compressor walks the complete directory tree with `Path.rglob()` and processes supported files concurrently.

Hidden files and the compressor checkpoint file are ignored.

Supported media extensions:

### Images

```text
.jpg
.jpeg
.png
.webp
.bmp
.tiff
.tif
.gif
```

### Video

```text
.mp4
.mkv
.avi
.mov
.3gp
.webm
.flv
.wmv
.m4v
.ts
```

### Audio

```text
.mp3
.m4a
.aac
.wav
.flac
.ogg
.opus
.wma
.amr
.aiff
```

### PDF

```text
.pdf
```

### Office documents

```text
.docx
.pptx
.xlsx
.doc
.ppt
.xls
```

Office documents are currently **detected and skipped** rather than modified.

---

# Compression Engine

## Images

Image processing uses Pillow.

### JPEG / JPG

The compressor:

- preserves EXIF data when available
- converts incompatible alpha/paletted modes to RGB
- enables Pillow optimization
- uses progressive JPEG encoding
- uses quality `82`
- uses subsampling `2`

Conceptually:

```text
JPEG input
   │
   ├── preserve EXIF when available
   ├── RGB normalization
   ├── optimize=True
   ├── quality=82
   └── progressive JPEG
```

### PNG

PNG files are rewritten with:

```text
compress_level=9
```

The result is retained only when it is smaller.

### WebP

WebP uses:

```text
quality=82
method=6
optimize=True
```

EXIF metadata is preserved when exposed by Pillow.

### GIF

GIF files are deliberately skipped:

```text
GIF skipped (animated risk)
```

This avoids blindly rewriting potentially animated GIFs.

### BMP

BMP files are converted to PNG **only when the PNG is smaller**.

The original:

```text
image.bmp
```

can become:

```text
image.png
```

The original BMP is removed only after the smaller PNG has been successfully created.

### TIFF / TIF

TIFF files are rewritten using:

```text
TIFF LZW compression
```

Again, the replacement is kept only when it reduces the file size.

---

# Video Compression

Video processing is powered directly by FFmpeg.

The engine first checks whether NVIDIA NVENC is usable by actually attempting a short FFmpeg encode.

If NVENC works, GPU encoding is selected unless `--cpu-video` is supplied.

### GPU path

HEVC:

```text
hevc_nvenc
```

H.264:

```text
h264_nvenc
```

The default GPU configuration includes:

```text
preset       = p5
tune         = hq
rate control = vbr
CQ           = 28 for HEVC
CQ           = 23 for H.264
spatial AQ   = enabled
temporal AQ  = enabled
lookahead    = 32
B-frames     = 3
pixel format = yuv420p
```

HEVC output is tagged:

```text
hvc1
```

This improves compatibility with players that expect the `hvc1` sample entry.

### CPU fallback

When NVENC is unavailable, the compressor falls back to:

```text
libx264
CRF = 22
preset = fast
threads = 0
```

Video output is normalized to MP4.

Audio is encoded as:

```text
AAC 128 kbps
```

and:

```text
-movflags +faststart
```

is used for MP4 output.

The original file is replaced only when the generated MP4 is smaller.

---

# Short-Video Deletion Rule

The project intentionally removes videos whose probed duration is:

```text
1.0 <= duration <= 3.0 seconds
```

This happens in both the compression and verification workflows.

The compressor treats these clips as disposable short artifacts and reports them separately:

```text
DELETED (1-3 second videos)
```

The verification utility also reports:

```text
DELETED SHORT VIDEOS
```

### Important

This is **not reversible through `restore_files.py`** because the restoration utility is designed around quarantined corrupt files, not deliberately deleted short videos.

If you do not want this behavior, modify the short-video deletion branch before running the project against important data.

---

# Audio Compression

Audio is converted to:

```text
.m4a
```

using AAC.

The encoder chooses settings based partly on filename/type.

## Speech / call recordings

If the filename contains:

```text
call recording
```

or:

```text
recording
```

the file is encoded as:

```text
AAC
32 kbps
mono
16 kHz
```

This is aggressively optimized for speech.

## Lossless / high-bandwidth source formats

For:

```text
.flac
.wav
.aiff
```

the output uses:

```text
AAC
192 kbps
44.1 kHz
```

## Other audio

Default:

```text
AAC
128 kbps
```

As with other conversions, the output must be smaller than the original before replacement.

---

# PDF Compression

PDF optimization uses Ghostscript.

Windows executable:

```text
gswin64c
```

Linux/macOS executable:

```text
gs
```

The PDF engine uses:

```text
-sDEVICE=pdfwrite
-dCompatibilityLevel=1.4
-dPDFSETTINGS=/ebook
-dDetectDuplicateImages=true
-dCompressFonts=true
```

The resulting PDF replaces the source only when it is smaller.

If Ghostscript is unavailable, the file is reported as an error rather than silently ignored.

---

# Duplicate Detection

Before compression, the engine performs duplicate analysis.

It uses a two-stage strategy:

```text
                    all files
                       │
                       ▼
                 group by size
                       │
             ┌─────────┴─────────┐
             │                   │
        unique size         same-size group
             │                   │
             │                   ▼
             │              SHA-256 hash
             │                   │
             │             ┌─────┴─────┐
             │             │           │
             │          unique      duplicate
             │             │           │
             └─────────────┘           ▼
                                  skip duplicate
```

This avoids hashing every file unnecessarily.

Only files sharing the same size are SHA-256 hashed.

Duplicate files are not deleted by the compressor. They are recorded as skipped:

```text
redundant duplicate of <original>
```

That distinction is important: duplicate detection is currently an **optimization filter**, not a deduplication/removal operation.

---

# Checkpoint / Resume System

The compression engine writes:

```text
compress_checkpoint.log
```

next to the executing compressor script.

Each completed file is appended to the checkpoint.

When the program starts again, it loads the checkpoint and skips paths already recorded.

This allows an interrupted large batch to resume rather than restarting from zero.

If the complete pending workload finishes successfully, the checkpoint file is removed.

Keyboard interruption is explicitly handled:

```text
Interrupted. Progress saved to checkpoint - safe to restart.
```

---

# Concurrency Architecture

The compressor uses two worker pools.

## Fast pool

Handles:

- images
- audio
- PDFs
- unsupported/office files

The default worker count is derived from:

```python
os.cpu_count()
```

## Video pool

Video work is intentionally isolated because FFmpeg video encoding is substantially heavier than ordinary filesystem/image operations.

The default video worker count is configurable.

This prevents a directory containing hundreds of videos from completely starving lightweight work.

The verifier follows the same architecture:

```text
                    workload
                       │
             ┌─────────┴─────────┐
             ▼                   ▼
        fast worker pool    video worker pool
        images/PDF/audio    video decode
```

Thread safety for shared counters and result collections is provided by a global `threading.Lock`.

---

# Integrity Verification

`verify_baaaaackup.py` is not a superficial "file exists" checker.

It attempts to determine whether supported files are actually decodable.

## Images

Pillow:

1. opens the image
2. reads dimensions
3. rejects zero dimensions
4. forces image loading with `img.load()`

This catches many damaged/truncated image files.

## Video

The verifier:

1. runs `ffprobe`
2. extracts format/stream metadata
3. resolves duration
4. validates the existence of a video stream
5. validates width and height
6. performs a complete FFmpeg decode to a null output

The critical verification operation is:

```text
ffmpeg -v error -i <file> -f null -
```

This means the file is actually decoded rather than merely inspected.

## Audio

The verifier:

1. runs FFprobe
2. confirms an audio stream exists
3. resolves duration where possible
4. performs a full FFmpeg decode

## PDF

Preferred engine:

```text
pikepdf
```

Fallback:

```text
Ghostscript -> nullpage renderer
```

If neither verification engine is available, the PDF may be skipped rather than falsely classified as valid.

---

# Corruption Quarantine

Files that fail verification are not immediately destroyed.

They are moved into:

```text
_Corrupted_Files/
```

A report is generated at:

```text
corrupt_files_report.txt
```

The report records the scanned directory, quarantine directory, deleted short videos, and failed files/reasons.

Example structure:

```text
your-folder/
├── photos/
├── videos/
├── _Corrupted_Files/
│   └── damaged_video.mp4
└── corrupt_files_report.txt
```

The quarantine directory is excluded from the verifier's scan.

If a filename collision occurs, the verifier generates an indexed destination name such as:

```text
video_0.mp4
video_1.mp4
```

---

# Restoration

`restore_files.py` reconstructs the original location of quarantined files using the paths recorded in:

```text
corrupt_files_report.txt
```

It:

1. reads the report
2. extracts `Original Path:` records
3. searches `_Corrupted_Files/`
4. recreates the original parent directory
5. moves the quarantined file back
6. reports restored and missing files
7. removes an empty quarantine directory

Usage:

```bash
python restore_files.py "D:\Samsung"
```

The restoration mechanism is therefore based on **path provenance recorded during quarantine**, not on filename guessing alone.

---

# Installation

## 1. Clone

```bash
git clone https://github.com/hacktivist211/bulk-compressor.git
cd bulk-compressor
```

## 2. Create a virtual environment

Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Install Python dependencies

```bash
python -m pip install -r requirements.txt
```

The core compression engine requires Pillow.

`pikepdf` is optional because PDF verification can fall back to Ghostscript.

---

# External Dependencies

## FFmpeg

Required for:

- video transcoding
- audio transcoding
- video verification
- audio verification
- FFprobe metadata extraction

The following commands should work:

```bash
ffmpeg -version
ffprobe -version
```

## Ghostscript

Required for:

- PDF compression
- fallback PDF verification

Windows:

```bash
gswin64c -version
```

Linux/macOS:

```bash
gs --version
```

## NVIDIA NVENC

Optional.

If an NVIDIA encoder is available and usable, the compressor automatically attempts GPU video encoding.

You can explicitly force CPU video processing:

```bash
python engine_compress.py "D:\Media" --cpu-video
```

The implementation probes NVENC rather than assuming that an NVIDIA GPU or driver automatically means NVENC is usable.

---

# Usage

## Compress a directory

```bash
python engine_compress.py "D:\Media"
```

Linux/macOS:

```bash
python3 engine_compress.py "/home/user/Media"
```

---

## Select video codec

HEVC:

```bash
python engine_compress.py "D:\Media" --codec hevc
```

H.264:

```bash
python engine_compress.py "D:\Media" --codec h264
```

---

## Control CQ

Example:

```bash
python engine_compress.py "D:\Media" --codec hevc --cq 30
```

Higher CQ generally means more aggressive quality reduction and potentially smaller output.

Lower CQ generally means higher quality and potentially larger output.

The actual result depends on the source material.

---

## Change NVENC preset

```bash
python engine_compress.py "D:\Media" --preset p6
```

The preset is passed to the NVENC video encoder when GPU mode is active.

---

## Control concurrent video encoders

```bash
python engine_compress.py "D:\Media" --video-workers 1
```

or:

```bash
python engine_compress.py "D:\Media" --video-workers 4
```

More concurrent video encodes do **not** automatically mean faster processing. GPU VRAM, encoder session limits, CPU scheduling, disk throughput, thermals, and source bitrate can become bottlenecks.

For large collections, start conservatively.

---

## Force CPU video encoding

```bash
python engine_compress.py "D:\Media" --cpu-video
```

This uses:

```text
libx264
CRF 22
preset fast
```

instead of NVENC.

---

# Verification

Run:

```bash
python verify_baaaaackup.py "D:\Media"
```

Linux/macOS:

```bash
python3 verify_baaaaackup.py "/home/user/Media"
```

The verifier prints status tags:

```text
[OK ]  fully decodable
[BAD]  corrupt/unplayable
[DEL]  1–3 second video deleted
[SKP]  unsupported or unavailable verification engine
```

---

# Restoration

If verification quarantines files:

```bash
python restore_files.py "D:\Media"
```

The script uses:

```text
D:\Media\corrupt_files_report.txt
D:\Media\_Corrupted_Files\
```

to restore the original paths.

---

# Recommended Production Workflow

For valuable data, do **not** start by running the compressor against the only copy.

Use:

```text
ORIGINAL DATA
     │
     ├── independent backup
     │
     ▼
BULK COMPRESSOR
     │
     ▼
VERIFY
     │
     ├── valid → retain
     │
     └── invalid → quarantine
                       │
                       ▼
                   restore if needed
```

A practical sequence is:

```bash
python engine_compress.py "D:\Media"
```

then:

```bash
python verify_baaaaackup.py "D:\Media"
```

and, if necessary:

```bash
python restore_files.py "D:\Media"
```

---

# Operational Safety

## This software modifies files in place

The compressor is not an export-to-new-directory tool.

Successful compression can replace the original file.

Examples:

```text
photo.jpg  -> rewritten photo.jpg
movie.mkv  -> movie.mp4
song.wav   -> song.m4a
document.pdf -> rewritten document.pdf
image.bmp  -> image.png
```

The source file is removed after successful conversion when an extension changes.

---

## The software intentionally deletes short videos

Files with durations from 1.0 through 3.0 seconds are deleted.

This behavior is currently part of the implementation.

If your dataset contains valuable short clips, **do not run the current version without modifying this rule or maintaining a separate backup**.

---

## Verification can move files

Failed files are moved to:

```text
_Corrupted_Files/
```

This is quarantine, not deletion.

The original path is retained in the verification report so the restoration utility can reconstruct it.

---

## Compression is lossy for several formats

The following are not mathematically lossless transformations:

- JPEG quality 82
- WebP quality 82
- video H.264/H.265 transcoding
- AAC audio transcoding
- speech audio downsampling/mono conversion
- PDF `/ebook` optimization may alter embedded media/image quality

The project optimizes **storage size**, not bit-for-bit identity.

---

# Metadata Behavior

Metadata preservation is format-dependent.

### Video

FFmpeg is invoked with:

```text
-map_metadata 0
```

so source metadata is mapped into the output where supported.

### Audio

The same metadata mapping approach is used.

### Images

JPEG/WebP EXIF is preserved when available through Pillow.

### PDF

Ghostscript rewrites the document; PDF metadata behavior should be treated as implementation-dependent.

Do not assume that every proprietary or application-specific metadata field survives transcoding.

---

# Why the "Only Keep Smaller" Rule Matters

Transcoding is not guaranteed to reduce size.

For example:

```text
source.mp4
   80 MB

generated.mp4
   92 MB
```

The compressor does not replace the source.

Instead:

```text
generated.mp4 -> discarded
source.mp4    -> retained
```

This makes the engine safer for already-compressed media collections.

The same policy is applied to normal image, audio, video, and PDF conversions.

---

# Performance Model

The implementation is designed for large filesystem workloads.

The main performance factors are:

```text
CPU
GPU / NVENC
RAM
storage I/O
FFmpeg codec complexity
source media characteristics
filesystem latency
number of concurrent workers
```

Duplicate detection reduces unnecessary compression work by eliminating byte-identical copies from the processing queue.

Video work is separated from lightweight operations because video encoding/decoding is significantly more computationally expensive.

For SSD-backed datasets, concurrency can improve throughput substantially.

For HDDs or network-mounted storage, excessive concurrency may instead increase contention.

---

# Output Accounting

At completion, the compressor reports:

```text
Files processed
Files compressed
Files skipped
Videos deleted
Errors
Total space saved
```

The reported space savings include:

- successful compression reductions
- deliberate 1–3 second video deletions

Duplicate files are skipped but are **not automatically deleted**, so duplicate detection does not contribute to deletion savings.

---

# Error Handling

The scripts attempt to fail locally rather than aborting the entire batch.

Examples include:

- FFmpeg failure
- FFmpeg timeout
- Ghostscript missing
- Ghostscript timeout
- Pillow missing
- output file is zero bytes
- output is not smaller
- extension collision
- failed filesystem replacement
- failed quarantine move
- failed restoration move

Errors are accumulated and displayed at the end of a run.

---

# Current Limitations

The current implementation has several intentional or architectural limitations.

### Office documents are not compressed

`.docx`, `.pptx`, `.xlsx`, `.doc`, `.ppt`, and `.xls` are detected but skipped.

The compressor currently reports:

```text
Office doc - skipped (libreoffice needed)
```

but does not invoke LibreOffice.

### GIF is skipped

GIF compression is skipped because animated GIF rewriting can introduce unwanted behavior.

### No dry-run mode

There is currently no built-in:

```text
--dry-run
```

mode.

### No automatic backup creation

The project does not create a separate backup copy before modifying files.

### No checksum manifest

SHA-256 is used internally for duplicate detection, but the project does not generate a permanent post-compression checksum manifest.

### No persistent database

State is stored using plain log/report files rather than SQLite or another database.

### Checkpoint identity is path-based

The compression checkpoint records file paths, not immutable content fingerprints.

If a file changes after being checkpointed, the current implementation does not independently prove that the path still refers to the same content.

### No AV1 compression path

The current video encoder selection is:

```text
HEVC NVENC
H.264 NVENC
libx264 CPU fallback
```

There is no AV1 encoder path in the current scripts.

### No progress UI

Progress is terminal-based and uses a single-line counter rather than a full progress-bar framework.

---

# File Structure

A minimal repository layout is:

```text
bulk-compressor/
├── engine_compress.py
├── verify_baaaaackup.py
├── restore_files.py
├── requirements.txt
└── README.md
```

Runtime-generated files/directories may appear inside processed collections:

```text
compress_checkpoint.log
corrupt_files_report.txt
_Corrupted_Files/
```

---

# Architecture at a Glance

```text
                         ┌─────────────────────┐
                         │   Target Directory   │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Recursive filesystem │
                         │       scan           │
                         └──────────┬──────────┘
                                    │
                                    ▼
                         ┌─────────────────────┐
                         │ Duplicate detection │
                         │ size → SHA-256      │
                         └──────────┬──────────┘
                                    │
                                    ▼
                  ┌─────────────────┴─────────────────┐
                  │                                   │
                  ▼                                   ▼
          Fast worker pool                     Video worker pool
                  │                                   │
        ┌─────────┼─────────┐                         │
        ▼         ▼         ▼                         ▼
      Images    Audio      PDFs                   FFmpeg
        │         │         │                         │
        └─────────┴─────────┘                         │
                  │                                   │
                  └─────────────────┬─────────────────┘
                                    ▼
                           smaller-than-source?
                              │           │
                            YES           NO
                              │           │
                              ▼           ▼
                         replace/rename  discard

                                    ▼
                              checkpoint
```

Verification:

```text
                      Processed Collection
                              │
                              ▼
                    ┌───────────────────┐
                    │ Type-specific test│
                    └─────────┬─────────┘
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
           Pillow          FFmpeg          pikepdf/GS
           images       video/audio           PDFs
              │               │                │
              └───────────────┼────────────────┘
                              ▼
                         PASS / FAIL
                              │
                         ┌────┴────┐
                         ▼         ▼
                        PASS      FAIL
                                  │
                                  ▼
                         _Corrupted_Files
                                  │
                                  ▼
                      corrupt_files_report.txt
```

---

# Design Principles

## Local-first

The project does not require a remote service.

## Conservative replacement

A generated artifact must beat the original size before normal replacement.

## Format-aware processing

Different media classes use different engines and quality models.

## Actual decode verification

Media is verified through decoding rather than relying only on file extensions or filesystem metadata.

## Recoverable quarantine

Verification failures are isolated instead of being immediately deleted.

## Parallelism with workload separation

Lightweight and video workloads use separate thread pools.

## Restartability

The compressor records completed paths and can resume interrupted work.

---

# Important Security / Data-Integrity Note

This tool operates directly on the filesystem and executes external programs:

```text
ffmpeg
ffprobe
Ghostscript
```

It should therefore be treated as a privileged filesystem-processing utility.

Run it only against directories you understand and have permission to modify.

For untrusted media collections, use a sandbox/container or a disposable working copy where appropriate.

Do not run it against:

```text
system directories
operating-system volumes
application installation directories
database storage
live production data
the only copy of irreplaceable personal data
```

---

# Development Notes

The Python implementation deliberately uses standard-library concurrency primitives:

```python
ThreadPoolExecutor
threading.Lock
subprocess
pathlib
hashlib
```

The heavy media operations are delegated to native executables rather than implemented in Python.

That architecture is appropriate because:

- FFmpeg is already highly optimized native media infrastructure.
- Pillow provides mature image codecs.
- Ghostscript provides a mature PDF rewriting engine.
- Python remains responsible for orchestration, scheduling, filesystem policy, reporting, and recovery.

---

# Troubleshooting

## `ffmpeg not found`

Install FFmpeg and ensure its `bin` directory is on `PATH`.

Verify:

```bash
ffmpeg -version
```

---

## `ffprobe failed`

Confirm FFprobe is installed alongside FFmpeg:

```bash
ffprobe -version
```

---

## `ghostscript not found in PATH`

Install Ghostscript and expose:

```text
gswin64c.exe
```

on Windows, or:

```text
gs
```

on Linux/macOS.

---

## Pillow not installed

```bash
python -m pip install Pillow
```

---

## PDF verification is skipped

Install:

```bash
python -m pip install pikepdf
```

or install Ghostscript.

---

## NVENC unavailable

Run:

```bash
python engine_compress.py "D:\Media" --cpu-video
```

The compressor can operate with CPU H.264 encoding when NVENC is unavailable.

---

# Example End-to-End Session

```powershell
git clone https://github.com/hacktivist211/bulk-compressor.git
cd bulk-compressor

python -m venv .venv
.venv\Scripts\activate

python -m pip install -r requirements.txt

ffmpeg -version
ffprobe -version
gswin64c -version

python engine_compress.py "D:\Samsung" --codec hevc --cq 28 --preset p5 --video-workers 2

python verify_baaaaackup.py "D:\Samsung"

# Only if quarantined files need to be restored:
python restore_files.py "D:\Samsung"
```

---

# License

No license information is inferred here because the supplied source files do not establish a project license.

Add the repository's actual license explicitly if/when one is selected.

---

# Repository

Source:

https://github.com/hacktivist211/bulk-compressor

If you find a reproducible failure, include:

- operating system
- Python version
- FFmpeg version
- FFprobe version
- Ghostscript version, if relevant
- exact command
- affected file format
- relevant terminal output

This makes codec, filesystem, and environment-specific failures substantially easier to diagnose.
