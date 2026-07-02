import os
import sys
import json
import shutil
import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tiff', '.tif', '.gif'}
VIDEO_EXTS = {'.mp4', '.mkv', '.avi', '.mov', '.3gp', '.webm', '.flv', '.wmv', '.m4v', '.ts'}
AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.wav', '.flac', '.ogg', '.opus', '.wma', '.amr', '.aiff'}
PDF_EXTS   = {'.pdf'}

_lock                = threading.Lock()
ok_files             = []
bad_files            = []
deleted_short_videos = []
skipped              = []
done_count           = 0
total_count          = 0

VIDEO_WORKERS = max(1, min(2, (os.cpu_count() or 2) // 2))
FAST_WORKERS  = max(4, os.cpu_count() or 4)


def fmt_bytes(n):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def tick(name, tag):
    global done_count
    with _lock:
        done_count += 1
        sys.stdout.write(f"\r[{done_count:>4}/{total_count}] [{tag}] {name[:65]:<65}")
        sys.stdout.flush()


def probe_format(path):
    r = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_format', '-show_streams', str(path)],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        return None, r.stderr[:300].strip()
    try:
        return json.loads(r.stdout), None
    except Exception as e:
        return None, str(e)


def full_decode(path, timeout):
    r = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'],
        capture_output=True, text=True, timeout=timeout
    )
    stderr = r.stderr.strip()
    if r.returncode != 0:
        return False, f"decode failed: {stderr[:300]}"
    if stderr:
        lines = [l for l in stderr.splitlines()
                 if not l.startswith('    Last message') and 'deprecated' not in l.lower()]
        if lines:
            return False, f"decode warnings/errors: {chr(10).join(lines[:5])}"
    return True, None


def verify_image(path):
    try:
        from PIL import Image
    except ImportError:
        with _lock:
            skipped.append((path, "Pillow not installed"))
        tick(path.name, "SKP")
        return

    try:
        with Image.open(path) as img:
            width, height = img.size
            mode = img.mode
            if width == 0 or height == 0:
                raise ValueError(f"zero dimension: {width}x{height}")
            img.load()

        with _lock:
            ok_files.append(path)
        tick(path.name, "OK ")
    except Exception as e:
        with _lock:
            bad_files.append((path, f"image decode failed: {e}"))
        tick(path.name, "BAD")


def verify_video(path):
    data, err = probe_format(path)
    if data is None:
        with _lock:
            bad_files.append((path, f"ffprobe failed: {err}"))
        tick(path.name, "BAD")
        return

    fmt = data.get('format', {})
    streams = data.get('streams', [])

    try:
        duration = float(fmt.get('duration', 0))
    except (ValueError, TypeError):
        duration = 0.0

    if duration == 0.0:
        for s in streams:
            try:
                duration = float(s.get('duration', 0))
                if duration > 0:
                    break
            except (ValueError, TypeError):
                continue

    if 1.0 <= duration <= 3.0:
        sz = path.stat().st_size
        path.unlink()
        with _lock:
            deleted_short_videos.append((path, f"{duration:.2f}s", sz))
        tick(path.name, "DEL")
        return

    video_streams = [s for s in streams if s.get('codec_type') == 'video']
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']

    if not video_streams:
        with _lock:
            bad_files.append((path, "no video stream found"))
        tick(path.name, "BAD")
        return

    vs = video_streams[0]
    width  = vs.get('width', 0)
    height = vs.get('height', 0)
    if not width or not height:
        with _lock:
            bad_files.append((path, f"invalid resolution: {width}x{height}"))
        tick(path.name, "BAD")
        return

    timeout = max(120, int(duration * 0.5)) if duration > 0 else 300
    ok, reason = full_decode(path, timeout)
    if not ok:
        with _lock:
            bad_files.append((path, reason))
        tick(path.name, "BAD")
        return

    with _lock:
        ok_files.append(path)
    tick(path.name, "OK ")


def verify_audio(path):
    data, err = probe_format(path)
    if data is None:
        with _lock:
            bad_files.append((path, f"ffprobe failed: {err}"))
        tick(path.name, "BAD")
        return

    streams = data.get('streams', [])
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
    if not audio_streams:
        with _lock:
            bad_files.append((path, "no audio stream found"))
        tick(path.name, "BAD")
        return

    try:
        duration = float(data.get('format', {}).get('duration', 0))
    except (ValueError, TypeError):
        duration = 0.0

    timeout = max(60, int(duration * 0.3)) if duration > 0 else 120
    ok, reason = full_decode(path, timeout)
    if not ok:
        with _lock:
            bad_files.append((path, reason))
        tick(path.name, "BAD")
        return

    with _lock:
        ok_files.append(path)
    tick(path.name, "OK ")


def verify_pdf(path):
    pikepdf_ok = False
    try:
        import pikepdf
        with pikepdf.open(path) as pdf:
            page_count = len(pdf.pages)
            if page_count == 0:
                raise ValueError("PDF has 0 pages")
            for page in pdf.pages:
                _ = page.get('/MediaBox')
        pikepdf_ok = True
        with _lock:
            ok_files.append(path)
        tick(path.name, "OK ")
        return
    except ImportError:
        pass
    except Exception as e:
        with _lock:
            bad_files.append((path, f"PDF corrupt (pikepdf): {e}"))
        tick(path.name, "BAD")
        return

    if not pikepdf_ok:
        try:
            gs_bin = 'gswin64c' if sys.platform == 'win32' else 'gs'
            r = subprocess.run(
                [gs_bin, '-dNOPAUSE', '-dBATCH', '-sDEVICE=nullpage',
                 '-dQUIET', str(path)],
                capture_output=True, text=True, timeout=120
            )
            if r.returncode != 0:
                with _lock:
                    bad_files.append((path, f"PDF render failed (gs): {r.stderr[:200].strip()}"))
                tick(path.name, "BAD")
                return
            with _lock:
                ok_files.append(path)
            tick(path.name, "OK ")
        except FileNotFoundError:
            with _lock:
                skipped.append((path, "neither pikepdf nor ghostscript available"))
            tick(path.name, "SKP")
        except subprocess.TimeoutExpired:
            with _lock:
                bad_files.append((path, "ghostscript timed out rendering PDF"))
            tick(path.name, "BAD")
        except Exception as e:
            with _lock:
                bad_files.append((path, str(e)))
            tick(path.name, "BAD")


def verify_file(path):
    if not path.exists():
        return

    try:
        size = path.stat().st_size
    except Exception:
        with _lock:
            bad_files.append((path, "could not stat file"))
        tick(path.name, "BAD")
        return

    if size == 0:
        with _lock:
            bad_files.append((path, "0-byte file"))
        tick(path.name, "BAD")
        return

    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        verify_image(path)
    elif ext in VIDEO_EXTS:
        verify_video(path)
    elif ext in AUDIO_EXTS:
        verify_audio(path)
    elif ext in PDF_EXTS:
        verify_pdf(path)
    else:
        with _lock:
            skipped.append((path, f"type {ext} not verifiable"))
        tick(path.name, "SKP")


def main():
    global total_count

    if len(sys.argv) < 2:
        print("Usage: python verify_backup.py <directory>")
        sys.exit(1)

    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        print(f"Directory not found: {root}")
        sys.exit(1)

    corrupt_dir = root / "_Corrupted_Files"

    all_files = [
        f for f in sorted(root.rglob('*'))
        if f.is_file() and not f.name.startswith('.') and corrupt_dir not in f.parents
    ]

    total_count = len(all_files)
    videos = [f for f in all_files if f.suffix.lower() in VIDEO_EXTS]
    fast   = [f for f in all_files if f.suffix.lower() not in VIDEO_EXTS]

    print(f"\nVerifying: {root}")
    print(f"Total files : {total_count}")
    print(f"Fast workers: {FAST_WORKERS}  |  Video workers: {VIDEO_WORKERS}")
    print(f"[OK ] = fully decodable  [BAD] = corrupt/unplayable  [DEL] = short video removed  [SKP] = skipped\n")

    fast_pool  = ThreadPoolExecutor(max_workers=FAST_WORKERS)
    video_pool = ThreadPoolExecutor(max_workers=VIDEO_WORKERS)

    all_futures = {}
    for f in fast:
        all_futures[fast_pool.submit(verify_file, f)] = f
    for f in videos:
        all_futures[video_pool.submit(verify_file, f)] = f

    try:
        for fut in as_completed(all_futures):
            exc = fut.exception()
            if exc:
                with _lock:
                    bad_files.append((all_futures[fut], f"unhandled: {exc}"))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        fast_pool.shutdown(wait=False, cancel_futures=True)
        video_pool.shutdown(wait=False, cancel_futures=True)
    else:
        fast_pool.shutdown(wait=True)
        video_pool.shutdown(wait=True)

    print("\n")
    print("=" * 80)
    print("VERIFICATION COMPLETE")
    print("=" * 80)
    print(f"  Total files           : {total_count}")
    print(f"  Passed (fully decoded): {len(ok_files)}")
    print(f"  Failed (bad/corrupt)  : {len(bad_files)}")
    print(f"  Deleted (1-3s videos) : {len(deleted_short_videos)}")
    print(f"  Skipped (no engine)   : {len(skipped)}")
    print("=" * 80)

    if deleted_short_videos:
        print(f"\nDELETED SHORT VIDEOS [{len(deleted_short_videos)}]")
        for path, dur, sz in deleted_short_videos:
            print(f"  {fmt_bytes(sz):>10}  ({dur})  {path}")

    if bad_files:
        corrupt_dir.mkdir(exist_ok=True)
        print(f"\nCORRUPT / UNPLAYABLE [{len(bad_files)}]  ->  moving to {corrupt_dir}")
        print("-" * 80)
        for idx, (path, reason) in enumerate(bad_files):
            try:
                size_str = fmt_bytes(path.stat().st_size)
            except Exception:
                size_str = "N/A"
            print(f"  {size_str:>10}  {path.name}")
            print(f"              {reason[:200]}")
            dest = corrupt_dir / path.name
            if dest.exists():
                dest = corrupt_dir / f"{path.stem}_{idx}{path.suffix}"
            try:
                shutil.move(str(path), str(dest))
            except Exception as e:
                print(f"              [MOVE ERROR] {e}")

        report = root / "corrupt_files_report.txt"
        with open(report, 'w', encoding='utf-8') as rf:
            rf.write(f"Verification Report\nScanned: {root}\nQuarantine: {corrupt_dir}\n")
            rf.write("=" * 80 + "\n\n")
            if deleted_short_videos:
                rf.write("DELETED SHORT VIDEOS\n")
                for path, dur, sz in deleted_short_videos:
                    rf.write(f"  [{dur}] {path}\n")
                rf.write("\n")
            rf.write("CORRUPT / UNPLAYABLE\n")
            for path, reason in bad_files:
                rf.write(f"  {path}\n  -> {reason}\n\n")
        print(f"\nReport saved: {report}")
    else:
        print("\nAll files passed full decode verification.")
        if deleted_short_videos:
            report = root / "corrupt_files_report.txt"
            with open(report, 'w', encoding='utf-8') as rf:
                rf.write(f"Verification Report\nScanned: {root}\n\nDELETED SHORT VIDEOS\n")
                for path, dur, sz in deleted_short_videos:
                    rf.write(f"  [{dur}] {path}\n")
            print(f"Deletion log saved: {report}")

    if skipped:
        print(f"\nSKIPPED [{len(skipped)}]")
        for path, reason in skipped:
            print(f"  {path.name}  ({reason})")
    print()


if __name__ == "__main__":
    main()