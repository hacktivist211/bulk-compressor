import hashlib
import importlib
import json
import multiprocessing
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Cross-platform abstraction: OS detection, CPU count, multiprocessing policy,
# process priority and per-OS performance tips. Every OS gets its fastest
# safe configuration automatically; override with BULK_MP_METHOD=fork|spawn|
# forkserver if you know better.
# ---------------------------------------------------------------------------

def detect_os():
    """Return 'windows', 'macos', 'linux' or the lowercased platform name."""
    name = platform.system().lower()
    return {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(
        name, name or "unknown")


def is_windows():
    return detect_os() == "windows"


def cpu_count():
    """Usable logical CPU count: process_cpu_count (3.13+), then affinity."""
    try:
        n = os.process_cpu_count()  # Python 3.13+, respects cgroups/containers
        if n:
            return n
    except AttributeError:
        pass
    try:
        n = len(os.sched_getaffinity(0))  # Linux: respects taskset/cgroup
        if n:
            return n
    except (AttributeError, OSError):
        pass
    return os.cpu_count() or 4


def mp_context_name():
    """Fastest safe multiprocessing start method for the current OS.

    windows -> spawn   (only option; also required for CUDA/loaded DLLs)
    macos   -> forkserver (fork is unsafe with the ObjC runtime and deprecated)
    linux   -> fork    (near-zero worker startup cost, copy-on-write memory)
    """
    override = os.environ.get("BULK_MP_METHOD", "").strip().lower()
    if override in ("fork", "spawn", "forkserver"):
        return override
    osname = detect_os()
    if osname == "windows":
        return "spawn"
    if osname == "macos":
        return "forkserver"
    return "fork"


def mp_context():
    return multiprocessing.get_context(mp_context_name())


def elevate_priority():
    """Raise this process' scheduling priority where permitted.

    Returns a short description for the run banner. Never raises.
    """
    try:
        if is_windows():
            import psutil  # optional; pip install psutil
            psutil.Process(os.getpid()).nice(psutil.HIGH_PRIORITY_CLASS)
            return "high (psutil)"
    except Exception:
        pass
    if not is_windows() and hasattr(os, "nice"):
        try:
            os.nice(-5)  # needs privileges on most systems; harmless if denied
            return "raised"
        except OSError:
            pass
        except AttributeError:
            pass
    return "default"


def os_tips(osname=None):
    """Platform-specific throughput tips printed once at startup."""
    name = osname or detect_os()
    if name == "windows":
        return [
            "add the target folder to Windows Security > Virus & threat protection "
            "> Exclusions (Defender real-time scanning can halve bulk I/O throughput)",
            "for paths longer than 260 chars enable long paths: registry key "
            "HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
            "LongPathsEnabled=1, or keep the tree under that limit",
        ]
    if name == "macos":
        return [
            "on Apple Silicon install tools natively (brew install sevenzip zstd "
            "ffmpeg libjxl) instead of running x86_64 builds under Rosetta 2",
            "grant Terminal/your IDE Full Disk Access for protected folders such "
            "as ~/Documents, ~/Desktop and external volumes",
        ]
    return [
        "for the fastest image pool, Linux uses fork-based workers; override with "
        "BULK_MP_METHOD=spawn if workers touch threads/CUDA",
    ]

TMP_PREFIX = ".bctmp_"
STORE_DIR = ".bulk_compressor"
QUARANTINE_DIR = "_Quarantine"
LEGACY_QUARANTINE_DIR = "_Corrupted_Files"
FINAL_STATUSES = ("replaced", "skipped", "kept", "incompressible", "unsupported", "duplicate", "rolled_back")

FAMILIES = {
    "jpeg": "image", "png": "image", "webp": "image", "gif": "image",
    "bmp": "image", "tiff": "image", "avif": "image", "heic": "image", "jxl": "image",
    "mp4": "video", "mkv": "video", "avi": "video", "ts": "video", "mpeg": "video", "flv": "video",
    "wav": "audio", "aiff": "audio", "flac": "audio", "ogg": "audio",
    "mp3": "audio", "amr": "audio",
    "pdf": "pdf",
    "zip": "archive", "gzip": "archive", "xz": "archive", "zstd": "archive",
    "bzip2": "archive", "7z": "archive", "rar": "archive",
    "sqlite": "generic", "elf": "generic", "exe": "generic",
    "text": "generic", "binary": "generic", "empty": "generic", "unreadable": "generic",
}

EXT_FAMILIES = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
    ".bmp": "image", ".tif": "image", ".tiff": "image", ".gif": "image",
    ".heic": "image", ".heif": "image", ".avif": "image", ".jxl": "image", ".ico": "image",
    ".mp4": "video", ".m4v": "video", ".mov": "video", ".mkv": "video", ".webm": "video",
    ".avi": "video", ".3gp": "video", ".3g2": "video", ".ts": "video", ".mts": "video",
    ".m2ts": "video", ".mpg": "video", ".mpeg": "video", ".flv": "video", ".wmv": "video",
    ".mp3": "audio", ".m4a": "audio", ".aac": "audio", ".wav": "audio", ".flac": "audio",
    ".ogg": "audio", ".opus": "audio", ".wma": "audio", ".amr": "audio",
    ".aiff": "audio", ".aif": "audio",
    ".pdf": "pdf",
    ".zip": "archive", ".gz": "archive", ".xz": "archive", ".zst": "archive",
    ".bz2": "archive", ".7z": "archive", ".rar": "archive",
    ".docx": "archive", ".xlsx": "archive", ".pptx": "archive", ".epub": "archive",
    ".odt": "archive", ".jar": "archive",
}


def which(tool):
    return shutil.which(tool)


def fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "N/A"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PiB" % n


def fmt_seconds(seconds):
    if seconds is None:
        return "--:--"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "--:--"
    if seconds < 0 or seconds > 31449600:
        return "--:--"
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def safe_stat(path):
    try:
        return os.stat(path)
    except Exception:
        return None


def safe_size(path):
    st = safe_stat(path)
    return st.st_size if st else 0


def file_key(path, st):
    return "%s|%d|%d" % (path, st.st_size, st.st_mtime_ns)


def hash_file(path, chunk_size=1 << 20):
    try:
        blake3 = importlib.import_module("blake3")
        h = blake3.blake3()
        algo = "blake3"
    except ImportError:
        h = hashlib.sha256()
        algo = "sha256"
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return algo, h.hexdigest()
    except OSError:
        return None, None


def same_file_snapshot(path, original_stat):
    current = safe_stat(path)
    if current is None:
        return False
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    return all(getattr(current, field, None) == getattr(original_stat, field, None)
               for field in fields)


def sniff_type(path):
    try:
        with open(path, "rb") as f:
            sample = f.read(4096)
    except OSError:
        return "unreadable"
    if not sample:
        return "empty"
    if sample.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        return "webp"
    if sample[:4] == b"RIFF" and sample[8:12] == b"AVI ":
        return "avi"
    if sample[:4] == b"RIFF" and sample[8:12] == b"WAVE":
        return "wav"
    if sample[:4] == b"FORM" and sample[8:12] == b"AIFF":
        return "aiff"
    if sample.startswith(b"GIF8"):
        return "gif"
    if sample.startswith(b"FLV"):
        return "flv"
    if sample.startswith(b"BM") and len(sample) >= 18:
        dib = int.from_bytes(sample[14:18], "little")
        if dib in (12, 40, 52, 56, 64, 108, 124):
            return "bmp"
    if sample[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if sample[4:8] == b"ftyp":
        brand = sample[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1", b"hevc"):
            return "heic"
        if brand == b"jxl ":
            return "jxl"
        return "mp4"
    if sample.startswith(b"\x00\x00\x00\x0cJXL "):
        return "jxl"
    if sample.startswith(b"\xff\x0a"):
        return "jxl"
    if sample.startswith(b"\x1a\x45\xdf\xa3"):
        return "mkv"
    if sample[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "mpeg"
    if sample[0:1] == b"\x47" and len(sample) >= 376 and sample[188:189] == b"\x47" and sample[376:377] == b"\x47":
        return "ts"
    if sample.startswith((b"%PDF",)):
        return "pdf"
    if sample.startswith(b"PK\x03\x04") or sample.startswith(b"PK\x05\x06"):
        return "zip"
    if sample.startswith(b"\x1f\x8b"):
        return "gzip"
    if sample.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if sample.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    if sample.startswith(b"BZh"):
        return "bzip2"
    if sample.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if sample.startswith(b"Rar!"):
        return "rar"
    if sample.startswith(b"fLaC"):
        return "flac"
    if sample.startswith(b"OggS"):
        return "ogg"
    if sample.startswith(b"ID3"):
        return "mp3"
    if len(sample) >= 2 and sample[0] == 0xFF and (sample[1] & 0xE0) == 0xE0 and (sample[1] & 0x06) != 0:
        return "mp3"
    if sample.startswith(b"#!AMR"):
        return "amr"
    if sample.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if sample.startswith(b"\x7fELF"):
        return "elf"
    if sample.startswith(b"MZ"):
        return "exe"
    if b"\x00" not in sample:
        printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
        if printable / len(sample) > 0.90:
            return "text"
    return "binary"


def family_of(kind):
    return FAMILIES.get(kind, "generic")


def family_of_ext(ext):
    return EXT_FAMILIES.get(str(ext).lower(), None)


def make_tmp(directory, suffix):
    fd, path = tempfile.mkstemp(prefix=TMP_PREFIX, suffix=suffix, dir=str(directory))
    os.close(fd)
    return Path(path)


def sweep_temp_files(root):
    removed = 0
    try:
        for p in Path(root).rglob(TMP_PREFIX + "*"):
            try:
                if p.is_file() and not p.is_symlink():
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


def scan_files(root, extra_exclude=()):
    root = Path(root)
    exclude = [root / STORE_DIR, root / QUARANTINE_DIR, root / LEGACY_QUARANTINE_DIR]
    exclude.extend(Path(e) for e in extra_exclude)
    files = []
    symlinks = 0
    for f in sorted(root.rglob("*")):
        try:
            if f.is_symlink():
                symlinks += 1
                continue
            if not f.is_file():
                continue
            if f.name.startswith("."):
                continue
            skip = False
            for d in exclude:
                if d == f.parent or d in f.parents:
                    skip = True
                    break
            if skip:
                continue
            files.append(f)
        except OSError:
            continue
    return files, symlinks


def preserve_stat(src_stat, dst):
    try:
        os.utime(str(dst), (src_stat.st_atime, src_stat.st_mtime))
    except OSError:
        pass
    try:
        os.chmod(str(dst), src_stat.st_mode & 0o7777)
    except OSError:
        pass


def atomic_replace(tmp, dst):
    tmp_path = Path(tmp)
    original_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    try:
        os.chmod(str(tmp_path), original_mode | stat.S_IWUSR)
        with open(str(tmp_path), "rb+") as f:
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.chmod(str(tmp_path), original_mode)
    os.replace(str(tmp_path), str(dst))
    try:
        fd = os.open(str(Path(dst).parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def zstd_backend():
    try:
        from compression import zstd as z
        return "stdlib", z
    except Exception:
        pass
    try:
        import zstandard as z
        return "zstandard", z
    except Exception:
        pass
    return None, None


def zstd_compress_bytes(mod, name, data, level):
    if name == "zstandard":
        return mod.ZstdCompressor(level=level).compress(data)
    if hasattr(mod, "compress"):
        return mod.compress(data, level)
    if hasattr(mod, "ZstdCompressor"):
        return mod.ZstdCompressor(level=level).compress(data)
    raise RuntimeError("zstd backend has no compress API")


def zstd_decompress_bytes(mod, name, data, max_output_size):
    if name == "zstandard":
        try:
            return mod.ZstdDecompressor().decompress(data, max_output_size=max_output_size)
        except TypeError:
            return mod.ZstdDecompressor().decompress(data)
    if hasattr(mod, "decompress"):
        try:
            return mod.decompress(data, max_output_size)
        except TypeError:
            return mod.decompress(data)
    if hasattr(mod, "ZstdDecompressor"):
        d = mod.ZstdDecompressor()
        try:
            return d.decompress(data, max_output_size=max_output_size)
        except TypeError:
            return d.decompress(data)
    raise RuntimeError("zstd backend has no decompress API")


def zstd_compress_file(src, dst, level=10):
    name, mod = zstd_backend()
    if name is None:
        return False, "zstd backend not available"
    try:
        if os.path.getsize(src) > (512 << 20):
            return False, "file exceeds the in-memory compression limit"
        data = Path(src).read_bytes()
        Path(dst).write_bytes(zstd_compress_bytes(mod, name, data, level))
        return True, None
    except Exception as e:
        return False, str(e)


def zstd_check(path):
    name, mod = zstd_backend()
    if name is None:
        return False, "zstd backend not available"
    try:
        data = Path(path).read_bytes()
        zstd_decompress_bytes(mod, name, data, 1 << 30)
        return True, None
    except Exception as e:
        return False, str(e)


def zstd_verify_roundtrip(src, dst):
    name, mod = zstd_backend()
    if name is None:
        return False, "zstd backend not available"
    try:
        expected = os.path.getsize(src)
        data = Path(dst).read_bytes()
        out = zstd_decompress_bytes(mod, name, data, expected + (1 << 20))
        if len(out) != expected:
            return False, "roundtrip size mismatch"
        h = hashlib.sha256()
        with open(src, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest() == hashlib.sha256(out).hexdigest(), "roundtrip hash mismatch"
    except Exception as e:
        return False, str(e)


def probe_video(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=90, check=False)
        if r.returncode != 0:
            return None, "", ""
        d = json.loads(r.stdout or "{}")
        streams = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
        if not streams:
            return None, "", ""
        try:
            dur = float(d.get("format", {}).get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if dur <= 0:
            for s in streams:
                try:
                    dur = float(s.get("duration") or 0)
                    if dur > 0:
                        break
                except (TypeError, ValueError):
                    continue
        return (dur or None), streams[0].get("pix_fmt", ""), streams[0].get("codec_name", "")
    except Exception:
        return None, "", ""


def nvenc_available(encoder):
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
             "-c:v", encoder, "-f", "null", "-"], capture_output=True, timeout=60, check=False)
        return r.returncode == 0
    except Exception:
        return False


def ffmpeg_encoders():
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                           capture_output=True, text=True, timeout=60, check=False)
        return set(r.stdout.split())
    except Exception:
        return set()


def exiftool_copy(src, dst):
    if which("exiftool") is None:
        return False
    try:
        r = subprocess.run(
            ["exiftool", "-q", "-q", "-m", "-P", "-overwrite_original",
             "-tagsfromfile", str(src), "-all:all", "-unsafe", "-icc_profile", str(dst)],
            capture_output=True, text=True, timeout=300, check=False)
        return r.returncode == 0
    except Exception:
        return False


def gs_binary():
    if sys.platform == "win32":
        for name in ("gswin64c", "gswin32c", "gs"):
            if which(name):
                return name
        return None
    return which("gs")


class Manifest:
    def __init__(self, root):
        self.lock = threading.Lock()
        self.root = Path(root)
        self.dir = self.root / STORE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.dir / "manifest.db"),
                                    check_same_thread=False, timeout=60)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            "key TEXT PRIMARY KEY, path TEXT, size INTEGER, mtime REAL, "
            "status TEXT, reason TEXT, out_path TEXT, out_size INTEGER, "
            "bytes_saved INTEGER, backup_path TEXT, ts REAL, backup_hash TEXT)")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(files)")}
        if "backup_hash" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN backup_hash TEXT")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS quarantine ("
            "orig_path TEXT PRIMARY KEY, q_path TEXT, reason TEXT, "
            "restored INTEGER DEFAULT 0, ts REAL)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS verify ("
            "key TEXT PRIMARY KEY, ok INTEGER, reason TEXT, ts REAL)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, "
            "started REAL, finished REAL, summary TEXT)")
        self.conn.commit()

    def final_status(self, key):
        with self.lock:
            cur = self.conn.execute("SELECT status FROM files WHERE key = ?", (key,))
            row = cur.fetchone()
        if row and row[0] in FINAL_STATUSES:
            return row[0]
        return None

    def final_keys(self):
        keys = set()
        with self.lock:
            cur = self.conn.execute("SELECT key, status FROM files")
            for key, status in cur.fetchall():
                if status in FINAL_STATUSES:
                    keys.add(key)
        return keys

    def set_file(self, key, path, size, mtime, status, reason,
                 out_path, out_size, bytes_saved, backup_path, backup_hash=None):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO files "
                "(key, path, size, mtime, status, reason, out_path, out_size, "
                "bytes_saved, backup_path, ts, backup_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, str(path), size, mtime, status, reason,
                 out_path, out_size, bytes_saved, backup_path, time.time(), backup_hash))
            self.conn.commit()

    def add_quarantine(self, orig_path, q_path, reason):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO quarantine (orig_path, q_path, reason, restored, ts) "
                "VALUES (?, ?, ?, 0, ?)",
                (str(orig_path), str(q_path), reason, time.time()))
            self.conn.commit()

    def quarantine_rows(self, unrestored_only=True):
        with self.lock:
            if unrestored_only:
                cur = self.conn.execute(
                    "SELECT orig_path, q_path, reason FROM quarantine WHERE restored = 0 ORDER BY orig_path")
            else:
                cur = self.conn.execute(
                    "SELECT orig_path, q_path, reason FROM quarantine ORDER BY orig_path")
            return cur.fetchall()

    def mark_quarantine_restored(self, orig_path):
        with self.lock:
            self.conn.execute("UPDATE quarantine SET restored = 1 WHERE orig_path = ?",
                              (str(orig_path),))
            self.conn.commit()

    def rollback_rows(self):
        with self.lock:
            cur = self.conn.execute(
                "SELECT key, path, size, out_path, backup_path, backup_hash FROM files "
                "WHERE status = 'replaced' AND backup_path IS NOT NULL ORDER BY path")
            return cur.fetchall()

    def mark_rolled_back(self, key):
        with self.lock:
            self.conn.execute("UPDATE files SET status = 'rolled_back' WHERE key = ?", (key,))
            self.conn.commit()

    def set_verify(self, key, ok, reason):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO verify (key, ok, reason, ts) VALUES (?, ?, ?, ?)",
                (key, 1 if ok else 0, reason, time.time()))
            self.conn.commit()

    def record_run(self, kind, started, finished, summary):
        with self.lock:
            self.conn.execute(
                "INSERT INTO runs (kind, started, finished, summary) VALUES (?, ?, ?, ?)",
                (kind, started, finished, json.dumps(summary)))
            self.conn.commit()

    def close(self):
        with self.lock:
            try:
                self.conn.commit()
                self.conn.close()
            except sqlite3.Error:
                pass


class EventLog:
    def __init__(self, root):
        self.lock = threading.Lock()
        self.dir = Path(root) / STORE_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"

    def log(self, event, **fields):
        rec = {"ts": round(time.time(), 3), "event": event}
        rec.update(fields)
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
