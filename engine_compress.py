import os
import sys
import json
import shutil
import hashlib
import tempfile
import argparse
import subprocess
import threading
from collections import defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.gif'}
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.3gp', '.webm', '.flv', '.wmv', '.m4v', '.ts'}
AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus', '.wma', '.amr', '.aiff'}
PDF_EXTS   = {'.pdf'}
DOC_EXTS   = {'.docx', '.pptx', '.xlsx', '.doc', '.ppt', '.xls'}

_lock            = threading.Lock()
errors           = []
skipped          = []
compressed_files = []
deleted_files    = []
total_saved      = 0
done_count       = 0
total_count      = 0

CPU = os.cpu_count() or 4
CFG = {'gpu': False, 'codec': 'hevc', 'cq': 28, 'preset': 'p5'}


def fmt_bytes(n):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def tick(name):
    global done_count
    with _lock:
        done_count += 1
        pct = done_count / total_count * 100
        sys.stdout.write(f"\r[{done_count:>4}/{total_count}] {pct:5.1f}%  {name[:60]:<60}")
        sys.stdout.flush()


def make_tmp(directory, suffix):
    fd, path = tempfile.mkstemp(suffix=suffix, dir=directory)
    os.close(fd)
    return Path(path)


def calculate_file_hash(path, chunk_size=1 << 20):
    hasher = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def find_duplicates(files):
    by_size = defaultdict(list)
    for f in files:
        try:
            size = f.stat().st_size
        except Exception:
            size = -1
        by_size[size].append(f)

    unique = []
    dupes = []
    to_hash = []
    for size, group in by_size.items():
        if size <= 0 or len(group) == 1:
            unique.extend(group)
        else:
            to_hash.extend(group)

    to_hash.sort()
    with ThreadPoolExecutor(max_workers=CPU) as pool:
        hashes = list(pool.map(calculate_file_hash, to_hash))

    seen = {}
    for f, h in zip(to_hash, hashes):
        if h is None:
            unique.append(f)
        elif h in seen:
            dupes.append((f, seen[h]))
        else:
            seen[h] = f
            unique.append(f)
    return unique, dupes


def probe_duration(path):
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(path)],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode != 0:
            return None
        return float(json.loads(r.stdout)['format']['duration'])
    except Exception:
        return None


def nvenc_available(encoder):
    try:
        r = subprocess.run(
            ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=320x240:d=0.2',
             '-c:v', encoder, '-f', 'null', '-'],
            capture_output=True, text=True, timeout=30
        )
        return r.returncode == 0
    except Exception:
        return False


def run_ffmpeg(cmd, timeout):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg timed out"
    except Exception as e:
        return False, str(e)
    if r.returncode != 0:
        return False, r.stderr[-400:].strip()
    return True, None


def video_cmd(path, tmp, use_gpu):
    cmd = ['ffmpeg', '-v', 'error', '-y']
    if use_gpu:
        cmd += ['-hwaccel', 'cuda']
    cmd += ['-i', str(path), '-map', '0:v:0', '-map', '0:a?', '-map_metadata', '0']
    if use_gpu:
        enc = 'hevc_nvenc' if CFG['codec'] == 'hevc' else 'h264_nvenc'
        cmd += ['-c:v', enc, '-preset', CFG['preset'], '-tune', 'hq',
                '-rc', 'vbr', '-cq', str(CFG['cq']), '-b:v', '0',
                '-spatial-aq', '1', '-temporal-aq', '1',
                '-rc-lookahead', '32', '-bf', '3', '-pix_fmt', 'yuv420p']
        if CFG['codec'] == 'hevc':
            cmd += ['-tag:v', 'hvc1']
    else:
        cmd += ['-c:v', 'libx264', '-crf', '22', '-preset', 'fast', '-threads', '0']
    cmd += ['-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(tmp)]
    return cmd


def replace_if_smaller(src, tmp_path, label, orig_stat=None, out_ext=None):
    global total_saved
    original_size = src.stat().st_size
    new_size = tmp_path.stat().st_size
    if new_size == 0:
        tmp_path.unlink(missing_ok=True)
        with _lock:
            errors.append((str(src), "output was 0 bytes"))
        return False
    if new_size < original_size:
        try:
            if orig_stat:
                os.utime(tmp_path, (orig_stat.st_atime, orig_stat.st_mtime))
            if out_ext and src.suffix.lower() != out_ext:
                dest = src.with_suffix(out_ext)
                if dest.exists() and dest != src:
                    tmp_path.unlink(missing_ok=True)
                    with _lock:
                        errors.append((str(src), f"collision: {dest} already exists"))
                    return False
                os.replace(str(tmp_path), str(dest))
                if dest != src:
                    src.unlink(missing_ok=True)
            else:
                os.replace(str(tmp_path), str(src))
            with _lock:
                total_saved += original_size - new_size
                compressed_files.append((str(src), original_size - new_size))
            return True
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            with _lock:
                errors.append((str(src), f"Failed to overwrite original file: {str(e)}"))
            return False
    else:
        tmp_path.unlink(missing_ok=True)
        with _lock:
            skipped.append((str(src), f"already optimal ({label})"))
        return False


def compress_image(path):
    global total_saved
    try:
        from PIL import Image
    except ImportError:
        with _lock:
            errors.append((str(path), "Pillow not installed"))
        return

    orig_stat = path.stat()
    ext = path.suffix.lower()
    tmp = make_tmp(path.parent, path.suffix)
    try:
        with Image.open(path) as img:
            exif = img.info.get('exif')
            save_kwargs = {'optimize': True}
            if exif:
                save_kwargs['exif'] = exif

            if ext in ('.jpg', '.jpeg'):
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGB')
                img.save(tmp, 'JPEG', quality=82, progressive=True, subsampling=2, **save_kwargs)
            elif ext == '.png':
                img.save(tmp, 'PNG', compress_level=9)
            elif ext == '.webp':
                img.save(tmp, 'WEBP', quality=82, method=6, **save_kwargs)
            elif ext == '.gif':
                tmp.unlink(missing_ok=True)
                with _lock:
                    skipped.append((str(path), "GIF skipped (animated risk)"))
                return
            elif ext == '.bmp':
                new_path = path.with_suffix('.png')
                img.save(tmp, 'PNG', compress_level=9)
                original_size = path.stat().st_size
                new_size = tmp.stat().st_size
                if new_size < original_size:
                    if new_path.exists():
                        tmp.unlink(missing_ok=True)
                        with _lock:
                            errors.append((str(path), f"collision: {new_path} already exists"))
                        return
                    try:
                        os.utime(tmp, (orig_stat.st_atime, orig_stat.st_mtime))
                        shutil.move(str(tmp), str(new_path))
                        path.unlink()
                        with _lock:
                            total_saved += original_size - new_size
                            compressed_files.append((str(path), original_size - new_size))
                    except Exception as e:
                        tmp.unlink(missing_ok=True)
                        with _lock:
                            errors.append((str(path), f"Failed to handle BMP file restructuring: {str(e)}"))
                else:
                    tmp.unlink(missing_ok=True)
                    with _lock:
                        skipped.append((str(path), "already optimal (bmp)"))
                return
            elif ext in ('.tiff', '.tif'):
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')
                img.save(tmp, 'TIFF', compression='tiff_lzw')
            else:
                tmp.unlink(missing_ok=True)
                with _lock:
                    skipped.append((str(path), f"unsupported image type {ext}"))
                return
        replace_if_smaller(path, tmp, "image", orig_stat=orig_stat)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        with _lock:
            errors.append((str(path), str(e)))


def compress_video(path):
    global total_saved
    orig_stat = path.stat()
    if orig_stat.st_size == 0:
        with _lock:
            skipped.append((str(path), "0-byte file"))
        return

    duration = probe_duration(path)
    if duration is not None and 1.0 <= duration <= 3.0:
        size = orig_stat.st_size
        try:
            path.unlink()
            with _lock:
                deleted_files.append((str(path), size))
                total_saved += size
            return
        except Exception as e:
            with _lock:
                errors.append((str(path), f"Deletion failed: {str(e)}"))
            return

    tmp = make_tmp(path.parent, '.mp4')
    try:
        ok, err = False, None
        if CFG['gpu']:
            ok, err = run_ffmpeg(video_cmd(path, tmp, True), 7200)
        if not ok:
            ok, err = run_ffmpeg(video_cmd(path, tmp, False), 7200)
        if not ok:
            tmp.unlink(missing_ok=True)
            with _lock:
                errors.append((str(path), err))
            return
        replace_if_smaller(path, tmp, "video", orig_stat=orig_stat, out_ext='.mp4')
    except Exception as e:
        tmp.unlink(missing_ok=True)
        with _lock:
            errors.append((str(path), str(e)))


def compress_audio(path):
    global total_saved
    orig_stat = path.stat()
    if orig_stat.st_size == 0:
        with _lock:
            skipped.append((str(path), "0-byte file"))
        return

    name_lower = path.stem.lower()
    is_speech = 'call recording' in name_lower or 'recording' in name_lower

    out_ext = '.m4a'
    tmp = make_tmp(path.parent, out_ext)

    if is_speech:
        audio_args = ['-c:a', 'aac', '-b:a', '32k', '-ac', '1', '-ar', '16000']
    elif path.suffix.lower() in ('.flac', '.wav', '.aiff'):
        audio_args = ['-c:a', 'aac', '-b:a', '192k', '-ar', '44100']
    else:
        audio_args = ['-c:a', 'aac', '-b:a', '128k']

    try:
        r = subprocess.run(
            ['ffmpeg', '-v', 'error', '-y', '-threads', '1', '-i', str(path),
             '-map', '0:a:0', '-map_metadata', '0', '-vn'] + audio_args +
             ['-movflags', '+faststart', str(tmp)],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            with _lock:
                errors.append((str(path), r.stderr[-400:].strip()))
            return

        if out_ext != path.suffix.lower():
            new_path = path.with_suffix(out_ext)
            original_size = orig_stat.st_size
            new_size = tmp.stat().st_size
            if new_size < original_size and new_size > 0:
                if new_path.exists():
                    tmp.unlink(missing_ok=True)
                    with _lock:
                        errors.append((str(path), f"collision: {new_path} already exists"))
                    return
                try:
                    os.utime(tmp, (orig_stat.st_atime, orig_stat.st_mtime))
                    shutil.move(str(tmp), str(new_path))
                    path.unlink()
                    with _lock:
                        total_saved += original_size - new_size
                        compressed_files.append((str(path), original_size - new_size))
                except Exception as e:
                    tmp.unlink(missing_ok=True)
                    with _lock:
                        errors.append((str(path), f"Failed to handle audio extension change: {str(e)}"))
            else:
                tmp.unlink(missing_ok=True)
                with _lock:
                    skipped.append((str(path), "already optimal (audio)"))
        else:
            replace_if_smaller(path, tmp, "audio", orig_stat=orig_stat)
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        with _lock:
            errors.append((str(path), "ffmpeg timed out"))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        with _lock:
            errors.append((str(path), str(e)))


def compress_pdf(path):
    orig_stat = path.stat()
    if orig_stat.st_size == 0:
        with _lock:
            skipped.append((str(path), "0-byte file"))
        return

    tmp = make_tmp(path.parent, '.pdf')
    try:
        gs_bin = 'gswin64c' if sys.platform == 'win32' else 'gs'
        r = subprocess.run(
            [gs_bin,
             '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4',
             '-dPDFSETTINGS=/ebook', '-dNOPAUSE', '-dQUIET', '-dBATCH',
             '-dDetectDuplicateImages=true', '-dCompressFonts=true',
             f'-sOutputFile={tmp}', str(path)],
            capture_output=True, text=True, timeout=300
        )
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            with _lock:
                errors.append((str(path), f"ghostscript error: {r.stderr[-300:].strip()}"))
            return
        replace_if_smaller(path, tmp, "pdf/ebook", orig_stat=orig_stat)
    except FileNotFoundError:
        tmp.unlink(missing_ok=True)
        with _lock:
            errors.append((str(path), "ghostscript not found in PATH"))
    except subprocess.TimeoutExpired:
        tmp.unlink(missing_ok=True)
        with _lock:
            errors.append((str(path), "ghostscript timed out"))
    except Exception as e:
        tmp.unlink(missing_ok=True)
        with _lock:
            errors.append((str(path), str(e)))


def process_file(path, checkpoint_path):
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        compress_image(path)
    elif ext in VIDEO_EXTS:
        compress_video(path)
    elif ext in AUDIO_EXTS:
        compress_audio(path)
    elif ext in PDF_EXTS:
        compress_pdf(path)
    elif ext in DOC_EXTS:
        with _lock:
            skipped.append((str(path), "Office doc - skipped (libreoffice needed)"))

    tick(path.name)

    with _lock:
        try:
            with open(checkpoint_path, 'a', encoding='utf-8') as cp:
                cp.write(str(path) + '\n')
        except Exception:
            pass


def safe_size(p):
    try:
        return p.stat().st_size
    except Exception:
        return 0


def main():
    global total_count

    ap = argparse.ArgumentParser()
    ap.add_argument('directory')
    ap.add_argument('--codec', choices=['hevc', 'h264'], default='hevc')
    ap.add_argument('--cq', type=int, default=None)
    ap.add_argument('--preset', default='p5')
    ap.add_argument('--video-workers', type=int, default=2)
    ap.add_argument('--cpu-video', action='store_true')
    args = ap.parse_args()

    root = Path(args.directory)
    if not root.is_dir():
        print(f"Directory not found: {root}")
        sys.exit(1)

    CFG['codec'] = args.codec
    CFG['cq'] = args.cq if args.cq is not None else (28 if args.codec == 'hevc' else 23)
    CFG['preset'] = args.preset

    encoder = 'hevc_nvenc' if args.codec == 'hevc' else 'h264_nvenc'
    if not args.cpu_video and nvenc_available(encoder):
        CFG['gpu'] = True
        video_workers = max(1, args.video_workers)
        fast_workers = max(2, CPU - video_workers - 1)
        video_mode = f"GPU {encoder} cq={CFG['cq']} preset={CFG['preset']}"
    else:
        video_workers = max(1, min(2, CPU // 2))
        fast_workers = max(4, CPU)
        video_mode = "CPU libx264 crf=22 (NVENC unavailable or disabled)"

    checkpoint_path = Path(sys.argv[0]).resolve().parent / 'compress_checkpoint.log'

    completed = set()
    if checkpoint_path.exists():
        try:
            with open(checkpoint_path, 'r', encoding='utf-8') as f:
                completed = {line.strip() for line in f if line.strip()}
            print(f"Resuming: {len(completed)} files already done, skipping them.")
        except Exception:
            pass

    print("Scanning directory tree and analyzing contents for redundancy...")
    all_files = [
        f for f in sorted(root.rglob('*'))
        if f.is_file() and not f.name.startswith('.') and f.name != 'compress_checkpoint.log'
    ]

    unique_files, dupes = find_duplicates(all_files)
    for f, original in dupes:
        skipped.append((str(f), f"redundant duplicate of {original.name}"))
    redundant_count = len(dupes)

    pending = [f for f in unique_files if str(f) not in completed]
    total_count = len(pending)
    already_done = len(all_files) - len(pending) - redundant_count

    print(f"\nTotal files discovered : {len(all_files)}")
    print(f"Redundant copies skipped: {redundant_count}")
    print(f"Already done            : {already_done}")
    print(f"To process              : {total_count}")
    print(f"Fast workers (CPU)      : {fast_workers}  (images / audio / pdf)")
    print(f"Video workers           : {video_workers}  [{video_mode}]")
    print(f"Checkpoint file         : {checkpoint_path}\n")

    if total_count == 0:
        print("Nothing left to process.")
        return

    videos = sorted((f for f in pending if f.suffix.lower() in VIDEO_EXTS), key=safe_size, reverse=True)
    fast   = sorted((f for f in pending if f.suffix.lower() not in VIDEO_EXTS), key=safe_size, reverse=True)

    video_pool = ThreadPoolExecutor(max_workers=video_workers)
    fast_pool  = ThreadPoolExecutor(max_workers=fast_workers)

    all_futures = {}
    for f in videos:
        all_futures[video_pool.submit(process_file, f, checkpoint_path)] = f
    for f in fast:
        all_futures[fast_pool.submit(process_file, f, checkpoint_path)] = f

    try:
        for fut in as_completed(all_futures):
            exc = fut.exception()
            if exc:
                with _lock:
                    errors.append((str(all_futures[fut]), f"unhandled: {exc}"))
    except KeyboardInterrupt:
        print("\n\nInterrupted. Progress saved to checkpoint - safe to restart.")
        fast_pool.shutdown(wait=False, cancel_futures=True)
        video_pool.shutdown(wait=False, cancel_futures=True)
        sys.exit(0)
    else:
        fast_pool.shutdown(wait=True)
        video_pool.shutdown(wait=True)

    print("\n")
    print("=" * 80)
    print("COMPRESSION COMPLETE")
    print("=" * 80)
    print(f"  Files processed    : {done_count}")
    print(f"  Files compressed   : {len(compressed_files)}")
    print(f"  Files skipped      : {len(skipped)}")
    print(f"  Videos deleted     : {len(deleted_files)}")
    print(f"  Errors             : {len(errors)}")
    print(f"  Total space saved  : {fmt_bytes(total_saved)}")
    print("=" * 80)

    if deleted_files:
        print(f"\nDELETED (1-3 second videos)  [{len(deleted_files)}]")
        for f, sz in deleted_files:
            print(f"  {fmt_bytes(sz):>10}  {f}")

    if errors:
        print(f"\nERRORS / NOT COMPRESSABLE  [{len(errors)}]")
        for f, reason in errors:
            print(f"  {f}")
            print(f"    -> {reason[:200]}")

    if skipped:
        print(f"\nSKIPPED (already optimal, redundant, or unsupported)  [{len(skipped)}]")
        for f, reason in skipped:
            print(f"  {f}  ({reason})")

    if done_count == total_count:
        if checkpoint_path.exists():
            try:
                checkpoint_path.unlink()
                print("\nAll done. Checkpoint file removed.")
            except Exception:
                pass

    print()


if __name__ == "__main__":
    main()
