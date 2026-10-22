                      
import argparse
import csv
import hashlib
import json
import math
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

from term_progress import (
    ProgressDisplay, run_ffmpeg_progress, enable_ansi, fmt_bytes, fmt_seconds,
)

CPU = os.cpu_count() or 4
GVFS = Path("/run/user/%d/gvfs" % os.getuid())
CACHE = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "phone-compress"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4", ".mov", ".3gp", ".m4v", ".mkv"}
EFFICIENT_EXTS = {".heic", ".heif", ".avif"}
WANTED_EXTS = IMAGE_EXTS | VIDEO_EXTS | EFFICIENT_EXTS
MOTION_MARKERS = (b"MotionPhoto", b"MicroVideo", b"SEFT", b"SEFH", b"hdrgm")
SKIP_DIRS = {"Android", "LOST.DIR", ".thumbnails", ".trashed", "_Quarantine"}
FAILED = {"error", "ioerror", "fatal"}
CHUNK = 8 << 20


class Ctx:
    pass


C = Ctx()


class Task:
    __slots__ = ("mount", "rel", "src", "size", "mtime", "atime", "ext", "is_video",
                 "key", "out_key", "dir", "orig", "out", "new_size", "budgeted",
                 "in_push", "dest")

    def __init__(self, mount, path, size, mtime, atime):
        self.mount = mount
        self.src = path
        self.rel = path.relative_to(mount)
        self.size = size
        self.mtime = mtime
        self.atime = atime
        self.ext = path.suffix.lower()
        self.is_video = self.ext in VIDEO_EXTS
        self.key = "%s|%d|%d" % (self.rel, size, int(mtime))
        self.out_key = "out:%s|%d" % (self.rel, size)
        self.dir = None
        self.orig = None
        self.out = None
        self.new_size = 0
        self.budgeted = False
        self.in_push = False
        self.dest = None


class Budget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0
        self.cv = threading.Condition()

    def acquire(self, n, stop):
        with self.cv:
            while self.used > 0 and self.used + n > self.limit:
                if stop.is_set():
                    return False
                self.cv.wait(timeout=1.0)
            self.used += n
            return True

    def release(self, n):
        with self.cv:
            self.used -= n
            self.cv.notify_all()


class Checkpoint:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.done = set()
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.done.add(json.loads(line))
                    except Exception:
                        pass
        self.fh = open(path, "a", encoding="utf-8")

    def has(self, key):
        return key in self.done

    def add(self, *keys):
        with self.lock:
            for k in keys:
                if k not in self.done:
                    self.done.add(k)
                    self.fh.write(json.dumps(k, ensure_ascii=False) + "\n")
            self.fh.flush()

    def close(self):
        with self.lock:
            self.fh.flush()
            self.fh.close()


class JsonLog:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.fh = open(path, "a", encoding="utf-8")

    def write(self, event, **fields):
        rec = {"ts": round(time.time(), 3), "event": event}
        rec.update(fields)
        with self.lock:
            self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.fh.flush()

    def close(self):
        with self.lock:
            self.fh.flush()
            self.fh.close()


def fatal(msg):
    print("\n[ERROR] %s" % msg, file=sys.stderr)
    sys.exit(1)


def nfc(s):
    return unicodedata.normalize("NFC", s)


def copy_file(src, dst):
    with open(str(src), "rb") as fi, open(str(dst), "wb") as fo:
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(b)


def sha256_of(path):
    h = hashlib.sha256()
    with open(str(path), "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def copy_metadata(src, dst):
    try:
        r = subprocess.run(
            ["exiftool", "-q", "-q", "-m", "-P", "-overwrite_original",
             "-tagsfromfile", str(src), "-all:all", "-unsafe", "-icc_profile", str(dst)],
            capture_output=True, text=True, timeout=300, check=False)
    except Exception:
        return False
    return r.returncode == 0


def image_job(src, dst, ext, quality, atime, mtime):
    try:
        from PIL import Image
        with Image.open(src) as im:
            if getattr(im, "n_frames", 1) > 1:
                return "skip", "multi-frame image"
            im.load()
            size = im.size
            if ext in (".jpg", ".jpeg"):
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)
            elif ext == ".webp":
                im.save(dst, "WEBP", quality=quality, method=6)
            elif ext == ".png":
                im.save(dst, "PNG", optimize=True, compress_level=9)
            else:
                return "skip", "unsupported " + ext
        if not copy_metadata(src, dst):
            return "error", "exiftool metadata copy failed"
        os.utime(dst, (atime, mtime))
        with Image.open(dst) as chk:
            chk.load()
            if chk.size != size:
                return "error", "dimension mismatch after compression"
        return "ok", ""
    except Exception as e:
        return "error", "image error: %s" % e


def is_motion_or_hdr(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        data = f.read(4 << 20)
        if size > (32 << 20):
            f.seek(-(4 << 20), os.SEEK_END)
            data += f.read()
    return any(m in data for m in MOTION_MARKERS)


def probe(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120, check=False)
        if r.returncode != 0:
            return None
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams", [])
        v = [s for s in streams if s.get("codec_type") == "video"]
        a = [s for s in streams if s.get("codec_type") == "audio"]
        if not v:
            return None
        dur = 0.0
        try:
            dur = float(d.get("format", {}).get("duration") or 0)
        except (TypeError, ValueError):
            pass
        if dur <= 0:
            try:
                dur = float(v[0].get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0.0
        return {"dur": dur, "vcodec": v[0].get("codec_name", ""),
                "acodec": a[0].get("codec_name", "") if a else ""}
    except Exception:
        return None


def nvenc_available(encoder):
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.2",
             "-c:v", encoder, "-f", "null", "-"], capture_output=True, timeout=60, check=False)
        return r.returncode == 0
    except Exception:
        return False


def build_video_cmd(src, dst, info, mode):
    a = C.args
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if mode == "gpu_hw":
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-movflags", "+use_metadata_tags+faststart"]
    if mode in ("gpu_hw", "gpu_sw"):
        cmd += ["-c:v", "hevc_nvenc", "-preset", a.nvenc_preset, "-tune", "hq",
                "-rc", "vbr", "-cq", str(a.cq), "-b:v", "0", "-spatial-aq", "1",
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "libx265", "-crf", str(a.crf), "-preset", a.preset,
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
    if info["acodec"] == "aac":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd.append(str(dst))
    return cmd


def finish(t, status, note="", permanent=False, saved=0, keep_dir=False, extra_keys=()):
    if t.dir is not None and not keep_dir:
        shutil.rmtree(str(t.dir), ignore_errors=True)
    if t.budgeted:
        t.budgeted = False
        C.budget.release(t.size)
    if permanent and not C.args.dry_run:
        C.cp.add(t.key, *extra_keys)
    with C.lock:
        C.counts[status] = C.counts.get(status, 0) + 1
        C.results.append((status, str(t.rel), t.size, t.new_size, note))
        C.saved += saved
        if status == "ioerror":
            C.io_streak += 1
            if C.io_streak >= C.args.max_io_errors:
                C.disconnected = True
                C.stop.set()
        elif status != "cancelled":
            C.io_streak = 0
        if status == "fatal":
            C.stop.set()
    C.log.write("file", status=status, path=str(t.rel), note=note,
                old=t.size, new=t.new_size)
    C.display.advance(t.size, saved, status in FAILED, "%s  %s" % (status.upper(), t.rel.name))
    with C.cv:
        C.outstanding -= 1
        C.cv.notify_all()


def guarded(fn):
    def run(t):
        try:
            fn(t)
        except Exception as e:
            note = "%s: %s" % (type(e).__name__, e)
            if t.in_push:
                finish(t, "fatal", "%s - original kept at %s" % (note, t.orig), keep_dir=True)
            else:
                finish(t, "error", note)
    return run


def stage_pull(t):
    if C.stop.is_set():
        return finish(t, "cancelled")
    if not C.budget.acquire(t.size, C.stop):
        return finish(t, "cancelled")
    t.budgeted = True
    if C.stop.is_set():
        return finish(t, "cancelled")
    t.dir = Path(tempfile.mkdtemp(prefix="job_", dir=str(C.work)))
    t.orig = t.dir / ("orig" + t.ext)
    t.out = t.dir / ("out" + (".mp4" if t.is_video else t.ext))
    try:
        copy_file(t.src, t.orig)
        os.utime(str(t.orig), (t.atime, t.mtime))
        current = t.src.stat()
    except Exception as e:
        return finish(t, "ioerror", "pull failed: %s" % e)
    if t.orig.stat().st_size != t.size:
        return finish(t, "ioerror", "pull truncated, phone untouched")
    if current.st_size != t.size or abs(current.st_mtime - t.mtime) > 1e-6:
        return finish(t, "error", "phone file changed while it was being pulled")
    if not t.is_video and is_motion_or_hdr(t.orig):
        return finish(t, "skip", "motion photo or Ultra HDR", permanent=True)
    pool = C.vid_pool if t.is_video else C.img_pool
    pool.submit(guarded(stage_compress), t)


def encode_video(t, info, slot, mode):
    t.out.unlink(missing_ok=True)
    cmd = build_video_cmd(t.orig, t.out, info, mode)
    return run_ffmpeg_progress(
        cmd, info["dur"], C.display, slot, 10800, stop_event=C.stop)


def stage_compress(t):
    if C.stop.is_set():
        return finish(t, "cancelled")
    if t.is_video:
        info = probe(t.orig)
        if info is None:
            return finish(t, "error", "no video stream")
        if info["vcodec"] == "hevc" and not C.args.force_reencode:
            return finish(t, "skip", "already HEVC", permanent=True)
        modes = ["gpu_hw", "gpu_sw", "cpu"] if C.gpu else ["cpu"]
        slot = C.display.claim_slot(t.rel.name)
        ok, err = False, "no attempt"
        try:
            for mode in modes:
                if C.stop.is_set():
                    return finish(t, "cancelled")
                ok, err = encode_video(t, info, slot, mode)
                if ok:
                    break
        finally:
            C.display.release_slot(slot)
        if not ok:
            return finish(t, "error", "ffmpeg: %s" % (err or "failed")[-200:])
        if not copy_metadata(t.orig, t.out):
            return finish(t, "error", "exiftool metadata copy failed")
        os.utime(str(t.out), (t.atime, t.mtime))
        chk = probe(t.out)
        if chk is None:
            return finish(t, "error", "output has no video stream, original kept")
        if info["dur"] > 0 and abs(chk["dur"] - info["dur"]) > max(1.0, 0.02 * info["dur"]):
            return finish(t, "error", "duration mismatch %.1fs vs %.1fs, original kept"
                          % (chk["dur"], info["dur"]))
    else:
        fut = C.pix_pool.submit(image_job, str(t.orig), str(t.out), t.ext,
                                C.args.quality, t.atime, t.mtime)
        status, note = fut.result()
        if status != "ok":
            return finish(t, status, note, permanent=(status == "skip"))
    if C.stop.is_set():
        return finish(t, "cancelled", "interrupted before push")
    C.push_pool.submit(guarded(stage_push), t)


def backup_phone_original(src, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = sha256_of(src)
    candidate = destination
    if candidate.exists():
        if sha256_of(candidate) == digest:
            return candidate
        candidate = destination.with_name(
            destination.name + ".sha256-" + digest + ".backup")
        index = 1
        while candidate.exists() and sha256_of(candidate) != digest:
            candidate = destination.with_name(
                destination.name + ".sha256-" + digest + "." + str(index) + ".backup")
            index += 1
        if candidate.exists():
            return candidate
    fd, tmp_name = tempfile.mkstemp(prefix=".phone-backup-", dir=str(candidate.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        copy_file(src, tmp)
        if sha256_of(tmp) != digest:
            raise OSError("backup hash verification failed")
        os.replace(str(tmp), str(candidate))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return candidate


def stage_push(t):
    a = C.args
    old = t.size
    new = t.out.stat().st_size
    t.new_size = new
    if new == 0 or new >= old * (1.0 - a.min_gain):
        return finish(t, "nogain", "%s -> %s" % (fmt_bytes(old), fmt_bytes(new)), permanent=True)
    if C.stop.is_set():
        return finish(t, "cancelled")
    if a.backup and not a.dry_run:
        try:
            backup_phone_original(t.orig, a.backup / t.rel)
        except Exception as e:
            return finish(t, "error", "backup failed; phone left untouched: %s" % e)
    note = "%s -> %s" % (fmt_bytes(old), fmt_bytes(new))
    if a.dry_run:
        return finish(t, "would", note)
    try:
        cur = t.src.stat()
    except Exception as e:
        return finish(t, "ioerror", "phone file unreadable before push: %s" % e)
    if cur.st_size != old or abs(cur.st_mtime - t.mtime) > 1e-6:
        return finish(t, "error", "file changed on phone since pull, left alone")
    dest = t.src.with_suffix(".mp4") if t.ext == ".3gp" else t.src
    if dest != t.src and dest.exists():
        return finish(t, "error", "%s already exists on phone" % dest.name)
    t.dest = dest
    journal = {
        "src": str(t.src),
        "dest": str(dest),
        "old": old,
        "new": new,
        "old_sha256": sha256_of(t.orig),
        "new_sha256": sha256_of(t.out),
    }
    (t.dir / "journal.json").write_text(
        json.dumps(journal, ensure_ascii=False), encoding="utf-8")
    if C.stop.is_set():
        return finish(t, "cancelled", "interrupted before phone replacement")
    t.in_push = True
    try:
        t.src.unlink()
        copy_file(t.out, dest)
        if dest.stat().st_size != new:
            raise RuntimeError("size mismatch after push")
        if a.verify_push and sha256_of(dest) != sha256_of(t.out):
            raise RuntimeError("read-back hash mismatch")
    except Exception as e:
        try:
            if dest != t.src and dest.exists():
                dest.unlink()
            copy_file(t.orig, t.src)
            if t.src.stat().st_size != old:
                raise RuntimeError("restore size mismatch")
        except Exception as e2:
            return finish(t, "fatal", "PUSH AND RESTORE BOTH FAILED: %s / %s - original is at %s"
                          % (e, e2, t.orig), keep_dir=True)
        t.in_push = False
        (t.dir / "journal.json").unlink(missing_ok=True)
        return finish(t, "error", "push failed (%s), original restored on phone" % e)
    t.in_push = False
    try:
        os.utime(str(dest), (t.atime, t.mtime))
        after = dest.stat()
        if int(after.st_mtime) != int(t.mtime):
            raise OSError("mtime not applied")
    except Exception:
        with C.lock:
            C.mtime_unfixed += 1
    rel_out = str(dest.relative_to(t.mount))
    finish(t, "compressed", note, permanent=True, saved=old - new,
           extra_keys=("out:%s|%d" % (rel_out, new),))


def recover_leftovers(work):
    if not work.is_dir():
        return
    for d in sorted(work.glob("job_*")):
        if not d.is_dir():
            continue
        jp = d / "journal.json"
        if not jp.exists():
            if any(d.glob("orig*")):
                print("[recover] no journal for %s; preserved staged original in %s" % (d.name, d))
                continue
            shutil.rmtree(str(d), ignore_errors=True)
            continue
        try:
            j = json.loads(jp.read_text(encoding="utf-8"))
            src, dest = Path(j["src"]), Path(j["dest"])
            old, new = int(j["old"]), int(j["new"])
            old_hash, new_hash = j.get("old_sha256"), j.get("new_sha256")
            origs = sorted(d.glob("orig*"))
            if not src.parent.is_dir():
                print("[recover] phone not mounted for %s, kept %s" % (src, d))
                continue
            original = origs[0] if origs else None
            if original is not None and old_hash and sha256_of(original) != old_hash:
                print("[recover] local original hash mismatch for %s; kept %s" % (src, d))
                continue
            src_exists = src.is_file()
            src_size = src.stat().st_size if src_exists else None
            src_hash = sha256_of(src) if src_exists and (old_hash or new_hash) else None
            dest_exists = dest.is_file()
            dest_size = dest.stat().st_size if dest_exists else None
            dest_hash = sha256_of(dest) if dest_exists and new_hash else None
            source_is_original = bool(
                old_hash and src_exists and src_size == old and
                src_hash == old_hash)
            destination_is_output = (
                dest_exists and dest_size == new and new_hash and
                dest_hash == new_hash)
            if dest == src and source_is_original:
                print("[recover] original still intact for %s" % src.name)
                shutil.rmtree(str(d), ignore_errors=True)
                continue
            if destination_is_output and (dest == src or not src_exists):
                print("[recover] verified completed push for %s" % src.name)
                shutil.rmtree(str(d), ignore_errors=True)
                continue
            if source_is_original and not dest_exists:
                print("[recover] original still intact for %s" % src.name)
                shutil.rmtree(str(d), ignore_errors=True)
                continue
            if original is None or not old_hash:
                print("[recover] cannot prove recovery copy integrity for %s; kept %s" % (src, d))
                continue
            if source_is_original:
                print("[recover] both source and destination need review for %s; kept %s" % (src, d))
                continue
            copy_file(original, src)
            if src.stat().st_size != old or sha256_of(src) != old_hash:
                print("[recover] restore verification failed for %s; kept %s" % (src, d))
                continue
            if dest != src and dest_exists:
                if destination_is_output:
                    print("[recover] restored original and retained verified output %s; kept %s" % (dest, d))
                    continue
                print("[recover] restored original; destination conflict retained at %s; kept %s" % (dest, d))
                continue
            print("[recover] restored original %s" % src)
            shutil.rmtree(str(d), ignore_errors=True)
        except Exception as e:
            print("[recover] failed for %s: %s (kept %s)" % (d.name, e, d))


def list_mounts():
    if not GVFS.is_dir():
        return []
    return sorted(p for p in GVFS.glob("mtp:host=*") if p.is_dir())


def gio_mount_all():
    if shutil.which("gio") is None:
        return
    try:
        r = subprocess.run(["gio", "mount", "-li"], capture_output=True, text=True, timeout=30, check=False)
    except Exception:
        return
    uris = sorted(set(re.findall(r"activation_root=(mtp://\S+)", r.stdout)))
    for uri in uris:
        try:
            subprocess.run(["gio", "mount", uri], capture_output=True, text=True, timeout=60, check=False)
        except Exception:
            pass
    if uris:
        time.sleep(1.5)


def parse_target(raw):
    s = raw.strip()
    m = re.match(r"^(mtp|gphoto2):/*(.*)$", s)
    if m:
        if m.group(1) == "gphoto2":
            fatal("PTP/iPhone devices are not supported")
        parts = [unquote(p) for p in m.group(2).split("/") if p]
        return {"kind": "uri", "device": parts[0] if parts else None, "parts": parts[1:]}
    if s.startswith("/"):
        return {"kind": "abs", "path": Path(s).expanduser().resolve(strict=False)}
    parts = [p for p in s.split("/") if p]
    if any(p in (".", "..") for p in parts):
        fatal("path traversal components are not allowed in target paths")
    return {"kind": "rel", "device": None, "parts": parts}


def select_mount(roots, hint):
    if len(roots) == 1:
        return roots[0]
    if hint:
        h = nfc(hint).lower().replace(" ", "_")
        sel = [r for r in roots if h in nfc(r.name).lower()]
        if len(sel) == 1:
            return sel[0]
    for r in roots:
        print("  ", r.name)
    fatal("cannot pick a device, pass --device <substring of the mount name above>")


def match_child(parent, name):
    want = nfc(name)
    try:
        for c in os.listdir(str(parent)):
            if nfc(c) == want:
                return c
    except Exception:
        return None
    return None


def resolve_targets(raw_paths, args):
    if args.mount_root:
        roots = [Path(args.mount_root).expanduser().resolve()]
        if not roots[0].is_dir():
            fatal("--mount-root %s is not a directory" % roots[0])
    else:
        roots = list_mounts()
        if not roots:
            gio_mount_all()
            roots = list_mounts()
        if not roots:
            fatal("no MTP mount under %s. Close Dolphin/other MTP clients, unlock the phone, set USB "
                  "to File Transfer, then: gio mount -li  and  gio mount mtp://<id>/" % GVFS)
    mount = None
    out = []
    for raw in raw_paths:
        t = parse_target(raw)
        if t["kind"] == "abs":
            hit = [r for r in roots if t["path"] == r or r in t["path"].parents]
            if not hit:
                fatal("%s is not under a known mount (%s)" % (t["path"], ", ".join(map(str, roots))))
            m, tdir = hit[0], t["path"]
        else:
            m = select_mount(roots, args.device or t["device"])
            parts = list(t["parts"])
            storages = sorted(c for c in os.listdir(str(m)) if (m / c).is_dir())
            first = match_child(m, parts[0]) if parts else None
            if first is not None and first in storages:
                storage = first
                parts = parts[1:]
            elif args.storage:
                sel = [s for s in storages if nfc(args.storage).lower() in nfc(s).lower()]
                if len(sel) != 1:
                    fatal("--storage %r matches %s" % (args.storage, sel))
                storage = sel[0]
            elif len(storages) == 1:
                storage = storages[0]
            else:
                for s in storages:
                    print("  ", s)
                fatal("multiple storages, pass --storage <substring> or include the storage name")
            tdir = m / storage
            for comp in parts:
                c = match_child(tdir, comp)
                if c is None:
                    fatal("not found on device: %s under %s" % (comp, tdir))
                tdir = tdir / c
        if not tdir.is_dir():
            fatal("not a directory: %s" % tdir)
        if mount is not None and m != mount:
            fatal("all paths must be on the same device")
        mount = m
        if tdir not in out:
            out.append(tdir)
    compact = []
    for candidate in sorted(out, key=lambda p: len(p.parts)):
        if not any(candidate == parent or parent in candidate.parents for parent in compact):
            compact.append(candidate)
    return mount, compact


def scan(tdirs):
    found = []
    other = 0
    for tdir in tdirs:
        for dirpath, dirnames, filenames in os.walk(str(tdir), topdown=True, onerror=lambda e: None):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
                and not (Path(dirpath) / d).is_symlink())
            for fn in sorted(filenames):
                full = Path(dirpath, fn)
                if fn.startswith(".") or full.is_symlink():
                    continue
                if Path(fn).suffix.lower() in WANTED_EXTS:
                    found.append(full)
                else:
                    other += 1
    return found, other


def stat_all(paths, workers):
    def one(p):
        try:
            st = p.stat()
            return p, st.st_size, st.st_mtime, st.st_atime
        except Exception:
            return p, None, None, None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, paths))


def interleave(a, b):
    out = []
    i = j = 0
    na, nb = len(a), len(b)
    while i < na or j < nb:
        if j >= nb or (i < na and i * nb <= j * na):
            out.append(a[i])
            i += 1
        else:
            out.append(b[j])
            j += 1
    return out


def wait_all():
    with C.cv:
        while C.outstanding > 0:
            C.cv.wait(timeout=0.5)


def main():
    ap = argparse.ArgumentParser(
        description="Pull media from an Android phone over MTP, compress in parallel on the PC, "
                    "push each result back to the exact same path.")
    ap.add_argument("paths", nargs="+",
                    help="mtp:/Device/Storage/DCIM/ style URIs, storage-relative paths (DCIM), "
                         "or absolute paths under the mount")
    ap.add_argument("--device", "--serial", dest="device",
                    help="substring of the mount name when several devices are connected")
    ap.add_argument("--storage", help="substring of the storage name when the phone has several")
    ap.add_argument("--mount-root", metavar="DIR",
                    help="use an already mounted MTP root (jmtpfs, simple-mtpfs) instead of gvfs")
    ap.add_argument("--work-dir", metavar="DIR", help="staging directory (default ~/.cache/phone-compress/work)")
    ap.add_argument("--backup", type=Path, metavar="DIR", help="keep every replaced original under DIR")
    ap.add_argument("--dry-run", action="store_true", help="pull and compress, never modify the phone")
    ap.add_argument("--limit", type=int, help="only process the first N candidates")
    ap.add_argument("--quality", type=int, default=82, help="JPEG/WebP quality")
    ap.add_argument("--crf", type=int, default=28, help="libx265 CRF")
    ap.add_argument("--preset", default="medium", help="libx265 preset")
    ap.add_argument("--cq", type=int, default=28, help="NVENC cq")
    ap.add_argument("--nvenc-preset", default="p5")
    ap.add_argument("--cpu", action="store_true", help="disable NVENC")
    ap.add_argument("--force-reencode", action="store_true", help="re-encode videos already in HEVC")
    ap.add_argument("--min-image", type=int, default=200_000)
    ap.add_argument("--min-video", type=int, default=1_000_000)
    ap.add_argument("--min-gain", type=float, default=0.02, help="minimum fractional saving to replace")
    ap.add_argument("--pull-workers", type=int, default=2)
    ap.add_argument("--push-workers", type=int, default=2)
    ap.add_argument("--image-workers", type=int, default=None)
    ap.add_argument("--video-workers", type=int, default=None)
    ap.add_argument("--stage-gb", type=float, default=8.0, help="max originals staged on disk at once")
    ap.add_argument("--verify-push", action="store_true", help="read back and hash every pushed file")
    ap.add_argument("--max-io-errors", type=int, default=8)
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()
    C.args = args
    if args.limit is not None and args.limit < 1:
        fatal("--limit must be at least 1")
    if not 1 <= args.quality <= 100:
        fatal("--quality must be between 1 and 100")
    if not 0 <= args.crf <= 51 or not 0 <= args.cq <= 51:
        fatal("--crf and --cq must be between 0 and 51")
    if args.min_image < 0 or args.min_video < 0:
        fatal("minimum file sizes cannot be negative")
    if not math.isfinite(args.min_gain) or not 0 <= args.min_gain < 1:
        fatal("--min-gain must be at least 0 and less than 1")
    for label, value in (("pull-workers", args.pull_workers),
                         ("push-workers", args.push_workers),
                         ("max-io-errors", args.max_io_errors)):
        if value < 1:
            fatal("--%s must be at least 1" % label)
    for label, value in (("image-workers", args.image_workers),
                         ("video-workers", args.video_workers)):
        if value is not None and value < 1:
            fatal("--%s must be at least 1" % label)
    if not math.isfinite(args.stage_gb) or args.stage_gb <= 0:
        fatal("--stage-gb must be greater than 0")

    for tool in ("ffmpeg", "ffprobe", "exiftool"):
        if shutil.which(tool) is None:
            fatal("'%s' not found: sudo dnf install ffmpeg perl-Image-ExifTool (ffmpeg needs RPM Fusion)" % tool)
    from importlib.util import find_spec
    if find_spec("PIL") is None:
        fatal("Pillow missing: conda install pillow")

    enable_ansi()
    mount, tdirs = resolve_targets(args.paths, args)
    serial = mount.name.split("host=", 1)[1] if "host=" in mount.name else mount.name
    safe_serial = re.sub(r"[^A-Za-z0-9._-]", "_", serial)

    CACHE.mkdir(parents=True, exist_ok=True)
    C.work = Path(args.work_dir).expanduser() if args.work_dir else CACHE / "work"
    C.work.mkdir(parents=True, exist_ok=True)
    recover_leftovers(C.work)
    free = shutil.disk_usage(str(C.work)).free
    if free < 10 << 30:
        print("[warn] only %s free under %s" % (fmt_bytes(free), C.work))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    C.log = JsonLog(CACHE / ("log-%s.jsonl" % stamp))
    C.cp = Checkpoint(CACHE / ("checkpoint-%s.jsonl" % safe_serial))
    C.gpu = (not args.cpu) and nvenc_available("hevc_nvenc")

    print("device  : %s" % serial)
    for d in tdirs:
        print("target  : %s" % d.relative_to(mount))
    print("mode    : %s" % ("DRY RUN (phone untouched)" if args.dry_run else "IN-PLACE (originals replaced)"))
    print("video   : %s" % (("NVENC cq=%d" % args.cq) if C.gpu else ("libx265 crf=%d preset=%s" % (args.crf, args.preset))))
    print("scanning...")

    paths, other = scan(tdirs)
    stats = stat_all(paths, 8)
    tasks = []
    unreadable = 0
    efficient = 0
    for p, size, mtime, atime in stats:
        if size is None:
            unreadable += 1
            continue
        if p.suffix.lower() in EFFICIENT_EXTS:
            efficient += 1
            continue
        tasks.append(Task(mount, p, size, mtime, atime))
    videos = sorted((t for t in tasks if t.is_video), key=lambda t: t.size, reverse=True)
    images = sorted((t for t in tasks if not t.is_video), key=lambda t: t.size, reverse=True)
    queue = interleave(videos, images)
    if args.limit:
        queue = queue[:args.limit]

    video_workers = args.video_workers or (3 if C.gpu else 2)
    image_workers = args.image_workers or (max(2, CPU - 2) if C.gpu else max(2, CPU // 2))
    print("scope   : %d images, %d videos (%d already efficient, %d other files, %d unreadable ignored)"
          % (len(images), len(videos), efficient, other, unreadable))
    print("workers : pull %d, image %d (processes), video %d, push %d, stage cap %.1f GiB\n"
          % (args.pull_workers, image_workers, video_workers, args.push_workers, args.stage_gb))

    C.lock = threading.Lock()
    C.cv = threading.Condition()
    C.stop = threading.Event()
    C.counts = {}
    C.results = []
    C.saved = 0
    C.io_streak = 0
    C.disconnected = False
    C.mtime_unfixed = 0
    C.outstanding = len(queue)
    C.budget = Budget(int(args.stage_gb * (1 << 30)))
    C.display = ProgressDisplay(len(queue), sum(t.size for t in queue), video_workers,
                                tty=(not args.no_progress) and sys.stdout.isatty())

    C.pix_pool = ProcessPoolExecutor(max_workers=image_workers,
                                     mp_context=multiprocessing.get_context("spawn"))
    C.pull_pool = ThreadPoolExecutor(max_workers=max(1, args.pull_workers), thread_name_prefix="pull")
    C.img_pool = ThreadPoolExecutor(max_workers=image_workers, thread_name_prefix="img")
    C.vid_pool = ThreadPoolExecutor(max_workers=video_workers, thread_name_prefix="vid")
    C.push_pool = ThreadPoolExecutor(max_workers=max(1, args.push_workers), thread_name_prefix="push")

    C.log.write("run_start", device=serial, targets=[str(d) for d in tdirs], dry_run=args.dry_run)
    started = time.time()
    C.display.redraw(force=True)

    pending = []
    for t in queue:
        if C.cp.has(t.key) or C.cp.has(t.out_key):
            finish(t, "resume")
        elif t.is_video and t.size < args.min_video:
            finish(t, "skip", "video too small", permanent=True)
        elif not t.is_video and t.size < args.min_image:
            finish(t, "skip", "image too small", permanent=True)
        else:
            pending.append(t)
    for t in pending:
        C.pull_pool.submit(guarded(stage_pull), t)

    interrupted = False
    try:
        wait_all()
    except KeyboardInterrupt:
        interrupted = True
        C.stop.set()
        try:
            wait_all()
        except KeyboardInterrupt:
            print("\nforced exit; leftover jobs are recovered on the next run")
            os._exit(130)

    for pool in (C.pull_pool, C.img_pool, C.vid_pool, C.push_pool):
        pool.shutdown(wait=True)
    C.pix_pool.shutdown(wait=True)
    C.display.finish()

    elapsed = time.time() - started
    print("\n" + "=" * 78)
    print("%s in %s   saved on phone: %s" % (
        "INTERRUPTED" if interrupted else ("STOPPED" if C.stop.is_set() else "COMPLETE"),
        fmt_seconds(elapsed), fmt_bytes(C.saved)))
    print("=" * 78)
    for k in sorted(C.counts):
        print("  %-12s %d" % (k, C.counts[k]))
    if C.disconnected:
        print("\n[!] repeated I/O failures, phone likely disconnected. Rerun to resume.")
    bad = [r for r in C.results if r[0] in FAILED]
    if bad:
        print("\nFAILED [%d]" % len(bad))
        for status, rel, old, new, note in bad[:40]:
            print("  %s: %s  (%s)" % (status, rel, note[:160]))
    if C.mtime_unfixed and not args.dry_run:
        print("\n[!] %d replaced files have a new modification time on the phone (MTP refused to set it). "
              "EXIF/creation_time are intact." % C.mtime_unfixed)

    report = CACHE / ("report-%s.csv" % stamp)
    with open(report, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "path", "old_bytes", "new_bytes", "note"])
        for status, rel, old, new, note in C.results:
            w.writerow([status, rel, old, new, note])
    print("\nreport  : %s" % report)
    print("log     : %s" % (CACHE / ("log-%s.jsonl" % stamp)))
    C.log.write("run_end", interrupted=interrupted, counts=C.counts, saved=C.saved)
    C.log.close()
    C.cp.close()
    if not args.dry_run and C.counts.get("compressed"):
        print("tip     : unplug and replug the phone if the gallery still shows old sizes")
    if C.stop.is_set() and not interrupted:
        sys.exit(2)


if __name__ == "__main__":
    main()
