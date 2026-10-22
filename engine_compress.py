import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.util import find_spec
from math import isfinite
from pathlib import Path
from typing import Any

from bc_common import (
    STORE_DIR,
    fmt_bytes, fmt_seconds, which, safe_stat, safe_size, file_key, hash_file,
    sniff_type, family_of, family_of_ext, make_tmp, sweep_temp_files, scan_files,
    atomic_replace, preserve_stat, same_file_snapshot, zstd_backend,
    zstd_compress_bytes, zstd_compress_file, zstd_verify_roundtrip,
    probe_video, nvenc_available, ffmpeg_encoders, exiftool_copy, gs_binary,
    Manifest, EventLog,
)
from term_progress import (
    ProgressDisplay, run_ffmpeg_progress, run_subprocess, enable_ansi,
)

CPU = os.cpu_count() or 4
CFG: dict[str, Any] = {}

DEFAULTS = {
    "lossless_only": False,
    "codec": "auto",
    "av1_crf": 32,
    "av1_preset": "5",
    "crf": 28,
    "x264_crf": 22,
    "preset": "medium",
    "cq": 28,
    "nvenc_preset": "p5",
    "film_grain": 0,
    "force_reencode": False,
    "cpu_video": False,
    "video_workers": None,
    "jpeg_quality": 82,
    "avif": False,
    "avif_quality": 60,
    "jxl": False,
    "audio_codec": "aac",
    "audio_bitrate": "128k",
    "pdf_settings": "/ebook",
    "generic_zstd": False,
    "zstd_level": 10,
    "deep_verify": False,
    "vmaf_samples": 2,
    "vmaf_threshold": 90.0,
    "skip_dedup": False,
    "backup": None,
}

BOOL_KEYS = {"lossless_only", "force_reencode", "cpu_video", "avif", "jxl",
             "generic_zstd", "deep_verify", "skip_dedup"}
INT_KEYS = {"av1_crf", "crf", "x264_crf", "cq", "film_grain", "video_workers",
            "jpeg_quality", "avif_quality", "zstd_level", "vmaf_samples"}
FLOAT_KEYS = {"vmaf_threshold"}

_lock = threading.Lock()
_replace_lock = threading.Lock()
results: list[dict[str, Any]] = []
total_saved = 0


def fatal(msg):
    print("\n[error] %s" % msg, file=sys.stderr)
    sys.exit(1)


def add_result(status, path, note, saved):
    global total_saved
    with _lock:
        results.append({"status": status, "path": str(path), "note": note, "saved": saved})
        total_saved += max(0, saved)


def config_search_paths():
    paths = []
    env = os.environ.get("BULK_COMPRESSOR_CONFIG")
    if env:
        paths.append(Path(env))
    paths.append(Path(__file__).resolve().parent / "config.json")
    if os.name == "posix":
        paths.append(Path.home() / ".config" / "bulk-compressor" / "config.json")
    else:
        base = os.environ.get("APPDATA")
        if base:
            paths.append(Path(base) / "bulk-compressor" / "config.json")
    return paths


def load_config(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        print("[warn] could not read config %s: %s" % (path, e))
        return {}
    if not isinstance(data, dict):
        print("[warn] config %s is not a JSON object; ignoring it" % path)
        return {}
    unknown = sorted(k for k in data if k not in DEFAULTS)
    if unknown:
        print("[warn] ignoring unknown config keys in %s: %s" % (path, ", ".join(unknown)))
    return {k: v for k, v in data.items() if k in DEFAULTS}


def resolve_settings(args, config):
    vals = {}
    for key, default in DEFAULTS.items():
        value = getattr(args, key, None)
        if value is None and key in config:
            value = config[key]
        if value is None:
            value = default
        if key in BOOL_KEYS and isinstance(value, str):
            value = str(value).strip().lower() in ("1", "true", "yes", "on")
        try:
            if key in INT_KEYS and value is not None:
                value = int(value)
            elif key in FLOAT_KEYS and value is not None:
                value = float(value)
        except (TypeError, ValueError):
            print("[warn] invalid value for %s, using default" % key)
            value = default
        vals[key] = value
    if vals["codec"] not in ("auto", "av1", "hevc", "h264"):
        print("[warn] invalid codec setting, using auto")
        vals["codec"] = "auto"
    if vals["audio_codec"] not in ("aac", "opus"):
        print("[warn] invalid audio_codec setting, using aac")
        vals["audio_codec"] = "aac"
    if vals["pdf_settings"] not in ("/ebook", "/screen", "/printer"):
        print("[warn] invalid pdf_settings, using /ebook")
        vals["pdf_settings"] = "/ebook"
    ranges = {
        "av1_crf": (0, 63),
        "crf": (0, 51),
        "x264_crf": (0, 51),
        "cq": (0, 51),
        "film_grain": (0, 15),
        "jpeg_quality": (1, 100),
        "avif_quality": (1, 100),
        "zstd_level": (1, 22),
        "vmaf_samples": (0, 100),
    }
    for key, (minimum, maximum) in ranges.items():
        value = vals[key]
        if value is None or not minimum <= value <= maximum:
            print("[warn] %s must be between %d and %d; using default" %
                  (key, minimum, maximum))
            vals[key] = DEFAULTS[key]
    threshold = vals["vmaf_threshold"]
    if not isfinite(threshold) or not 0 <= threshold <= 100:
        print("[warn] vmaf_threshold must be between 0 and 100; using default")
        vals["vmaf_threshold"] = DEFAULTS["vmaf_threshold"]
    if vals["video_workers"] is not None and vals["video_workers"] < 1:
        print("[warn] video_workers must be positive; using automatic selection")
        vals["video_workers"] = None
    return vals


def pikepdf_available():
    return find_spec("pikepdf") is not None


def pillow_avif_available():
    try:
        from PIL import features
        return features.check("avif")
    except Exception:
        return False


def find_duplicates(files):
    by_size = defaultdict(list)
    for f in files:
        by_size[safe_size(f)].append(f)
    unique = []
    dupes = []
    to_hash = []
    for size, group in by_size.items():
        if size <= 0 or len(group) == 1:
            unique.extend(group)
        else:
            to_hash.extend(group)
    to_hash.sort(key=str)
    if to_hash:
        with ThreadPoolExecutor(max_workers=min(CPU, 8)) as pool:
            digests = list(pool.map(lambda p: hash_file(p)[1], to_hash))
    else:
        digests = []
    seen = {}
    for f, digest in zip(to_hash, digests):
        if digest is None:
            unique.append(f)
        elif digest in seen:
            dupes.append((f, seen[digest]))
        else:
            seen[digest] = f
            unique.append(f)
    return unique, dupes


def save_with_meta(im, tmp, fmt, **base):
    kwargs = dict(base)
    exif = im.info.get("exif")
    icc = im.info.get("icc_profile")
    if exif:
        kwargs["exif"] = exif
    if icc:
        kwargs["icc_profile"] = icc
    try:
        im.save(str(tmp), fmt, **kwargs)
    except TypeError:
        im.save(str(tmp), fmt, **base)


def pixel_compare(src, dst):
    def check():
        try:
            from PIL import Image
            with Image.open(str(src)) as a, Image.open(str(dst)) as b:
                if a.size != b.size or a.mode != b.mode:
                    return False, "dimensions or mode differ"
                return a.tobytes() == b.tobytes(), "pixel data differs"
        except Exception as e:
            return False, str(e)
    return check


def pcm_compare(src, dst):
    def check():
        try:
            digests = []
            for p in (src, dst):
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(p), "-map", "0:a:0", "-f", "md5", "-"],
                    capture_output=True, text=True, timeout=1800, check=False)
                if r.returncode != 0:
                    return False, "decode failed during comparison"
                digests.append(r.stdout.strip())
            return digests[0] == digests[1], "decoded PCM differs"
        except Exception as e:
            return False, str(e)
    return check


def jxl_roundtrip(src, jxl_path, djxl):
    def check():
        try:
            tmp = make_tmp(Path(src).parent, ".jpg")
            r = subprocess.run([djxl, str(jxl_path), str(tmp)],
                               capture_output=True, text=True, timeout=3600, check=False)
            if r.returncode != 0:
                tmp.unlink(missing_ok=True)
                return False, "djxl failed"
            same = tmp.read_bytes() == Path(src).read_bytes()
            tmp.unlink(missing_ok=True)
            return same, "reconstructed JPEG differs from original"
        except Exception as e:
            return False, str(e)
    return check


def file_hash_token(path):
    algo, digest = hash_file(path)
    if algo is None or digest is None:
        raise OSError("could not hash %s" % path)
    return "%s:%s" % (algo, digest)


def create_backup(path, backup_root, original_stat):
    candidate = backup_root
    candidate.parent.mkdir(parents=True, exist_ok=True)
    source_token = file_hash_token(path)
    if candidate.exists():
        if file_hash_token(candidate) == source_token:
            return candidate, source_token
        suffix = "%d.%d.backup" % (original_stat.st_mtime_ns, original_stat.st_size)
        candidate = candidate.with_name(candidate.name + "." + suffix)
        index = 1
        while candidate.exists() and file_hash_token(candidate) != source_token:
            candidate = backup_root.with_name(
                backup_root.name + ".%s.%d" % (suffix, index))
            index += 1
        if candidate.exists():
            return candidate, source_token
    tmp = make_tmp(candidate.parent, candidate.suffix or ".backup")
    try:
        shutil.copyfile(str(path), str(tmp))
        if not same_file_snapshot(path, original_stat):
            raise RuntimeError("source changed while creating backup")
        if file_hash_token(tmp) != source_token:
            raise RuntimeError("backup verification failed")
        preserve_stat(original_stat, tmp)
        atomic_replace(tmp, candidate)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return candidate, source_token


def finalize(ctx, path, st, tmp, out_ext, label, deep_check=None):
    key = ctx.get("key")
    if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
        tmp.unlink(missing_ok=True)
        return ("cancelled", "interrupted before replacement", 0)
    if not same_file_snapshot(path, st):
        tmp.unlink(missing_ok=True)
        return ("error", "source changed during compression; original left untouched", 0)
    try:
        if deep_check is not None:
            ok, why = deep_check()
            if not ok:
                tmp.unlink(missing_ok=True)
                return ("error", "deep verification failed: %s" % why, 0)
        new_size = tmp.stat().st_size
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)
    if new_size == 0:
        tmp.unlink(missing_ok=True)
        return ("error", "output was 0 bytes", 0)
    if new_size >= st.st_size:
        tmp.unlink(missing_ok=True)
        return ("kept", "no size gain (%s)" % label, 0)
    backup_path = None
    backup_hash = None
    if ctx["backup"] is not None:
        try:
            rel = path.relative_to(ctx["root"])
            backup_path_obj, backup_hash = create_backup(
                path, ctx["backup"] / rel, st)
            backup_path = str(backup_path_obj)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return ("error", "backup copy failed: %s" % e, 0)
    if not same_file_snapshot(path, st):
        tmp.unlink(missing_ok=True)
        return ("error", "source changed before replacement; original left untouched", 0)
    preserve_stat(st, tmp)
    out_ext = out_ext.lower()
    final = path if path.suffix.lower() == out_ext else path.with_suffix(out_ext)
    with _replace_lock:
        if final != path and final.exists():
            tmp.unlink(missing_ok=True)
            return ("error", "collision: %s already exists" % final, 0)
        if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
            tmp.unlink(missing_ok=True)
            return ("cancelled", "interrupted before replacement", 0)
        if not same_file_snapshot(path, st):
            tmp.unlink(missing_ok=True)
            return ("error", "source changed immediately before replacement; original left untouched", 0)
        try:
            atomic_replace(tmp, final)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return ("error", "replace failed: %s" % e, 0)
        if final != path:
            try:
                path.unlink()
            except OSError as e:
                try:
                    final.unlink(missing_ok=True)
                except OSError:
                    return ("error", "output created but original could not be removed; both files preserved: %s" % e, 0)
                return ("error", "could not remove original; compressed output was discarded: %s" % e, 0)
    saved = st.st_size - new_size
    ctx["manifest"].set_file(
        key, str(path), st.st_size, st.st_mtime, "replaced", label,
        str(final), new_size, saved, backup_path, backup_hash)
    ctx["events"].log("replaced", path=str(path), out=str(final), saved=saved, label=label)
    return ("replaced", "%s -> %s (%s)" %
            (fmt_bytes(st.st_size), fmt_bytes(new_size), label), saved)


def build_video_cmd(cfg, src, dst, pix_fmt, force_cpu=False):
    gpu = cfg["gpu"] and not force_cpu
    deep = any(tag in (pix_fmt or "") for tag in ("p010", "10le", "10be", "12le", "12be"))
    cmd = ["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts"]
    if gpu:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-max_muxing_queue_size", "1024"]
    codec = cfg["codec"]
    if gpu:
        enc = {"hevc": "hevc_nvenc", "h264": "h264_nvenc", "av1": "av1_nvenc"}[codec]
        cmd += ["-c:v", enc, "-preset", cfg["nvenc_preset"], "-tune", "hq", "-rc", "vbr",
                "-cq", str(cfg["cq"]), "-b:v", "0", "-spatial-aq", "1",
                "-pix_fmt", "p010le" if deep else "yuv420p"]
        if codec == "hevc":
            cmd += ["-tag:v", "hvc1"]
    elif codec == "av1":
        params = ["tune=0"]
        if cfg["film_grain"]:
            params.append("film-grain=%d" % cfg["film_grain"])
            params.append("film-grain-denoise=0")
        cmd += ["-c:v", "libsvtav1", "-preset", str(cfg["av1_preset"]),
                "-crf", str(cfg["av1_crf"]), "-svtav1-params", ":".join(params),
                "-pix_fmt", "yuv420p10le" if deep else "yuv420p"]
    elif codec == "hevc":
        cmd += ["-c:v", "libx265", "-crf", str(cfg["crf"]), "-preset", cfg["preset"],
                "-pix_fmt", "yuv420p10le" if deep else "yuv420p", "-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "libx264", "-crf", str(cfg["x264_crf"]), "-preset", cfg["preset"],
                "-pix_fmt", "yuv420p"]
    if cfg["audio_codec"] == "opus":
        cmd += ["-c:a", "libopus", "-b:a", cfg["audio_bitrate"], "-strict", "experimental"]
    else:
        cmd += ["-c:a", "aac", "-b:a", cfg["audio_bitrate"]]
    cmd += ["-movflags", "+faststart+use_metadata_tags", str(dst)]
    return cmd


def vmaf_gate(cfg, src, dst, duration, stop_event=None):
    if cfg["vmaf_samples"] <= 0:
        return True, None
    if not duration or duration <= 0:
        return True, None
    seg = min(5.0, duration / 4.0)
    if seg < 0.5:
        return True, None
    scores = []
    for i in range(cfg["vmaf_samples"]):
        frac = (i + 1) / (cfg["vmaf_samples"] + 1)
        start = max(0.0, min(duration - seg, duration * frac - seg / 2.0))
        filt = ("[0:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[a];"
                "[1:v]trim=start=%.3f:end=%.3f,setpts=PTS-STARTPTS[b];"
                "[a][b]libvmaf=n_threads=2" % (start, start + seg, start, start + seg))
        cmd = ["ffmpeg", "-v", "info", "-i", str(src), "-i", str(dst),
               "-lavfi", filt, "-f", "null", "-"]
        try:
            r = run_subprocess(cmd, timeout=1800, stop_event=stop_event)
        except InterruptedError:
            return True, "cancelled"
        except (OSError, subprocess.SubprocessError):
            return True, "libvmaf unavailable, gate skipped"
        if stop_event is not None and stop_event.is_set():
            return True, "cancelled"
        if r.returncode != 0:
            return True, "libvmaf unavailable, gate skipped"
        for m in re.finditer(r"VMAF score[^0-9]*([0-9]+\.?[0-9]*)", r.stderr):
            scores.append(float(m.group(1)))
    if not scores:
        return True, "no VMAF score parsed, gate skipped"
    avg = sum(scores) / len(scores)
    if avg < cfg["vmaf_threshold"]:
        return False, "VMAF %.2f below threshold %.2f" % (avg, cfg["vmaf_threshold"])
    return True, "VMAF %.2f" % avg


def handle_image(path, st, ctx, kind):
    cfg = ctx["cfg"]
    if kind in ("heic", "avif", "jxl", "empty", "unreadable"):
        return ("skipped", "input is %s; left untouched" % kind, 0)
    animated = False
    if kind in ("gif", "webp", "png", "tiff"):
        try:
            from PIL import Image
            with Image.open(str(path)) as im:
                animated = getattr(im, "n_frames", 1) > 1
        except Exception as e:
            return ("error", "image unreadable: %s" % e, 0)
    if animated:
        if cfg["lossy"] and kind == "gif":
            return encode_animated_gif(path, st, ctx)
        return ("skipped", "multi-frame image, left untouched", 0)
    if cfg["avif"] and kind in ("png", "bmp", "tiff"):
        return convert_avif(path, st, ctx)
    if kind == "jpeg":
        if cfg["jxl"] and cfg["cjxl"]:
            return transcode_jxl(path, st, ctx)
        if cfg["avif"]:
            return convert_avif(path, st, ctx)
        if cfg["lossy"]:
            return reencode_jpeg(path, st, ctx)
        return ("skipped", "lossless-only mode leaves JPEG untouched; use --jxl or drop --lossless-only", 0)
    if kind == "webp":
        if cfg["lossy"]:
            return reencode_webp(path, st, ctx)
        return ("skipped", "lossless-only mode leaves WebP untouched", 0)
    if kind == "png":
        if cfg["avif"]:
            return convert_avif(path, st, ctx)
        return optimize_png(path, st, ctx)
    if kind == "bmp":
        return convert_raster(path, st, ctx, "PNG", ".png", "bmp to png")
    if kind == "tiff":
        return convert_raster(path, st, ctx, "TIFF", path.suffix.lower(), "tiff lzw")
    if kind == "gif":
        return convert_raster(path, st, ctx, "PNG", ".png", "static gif to png")
    return ("skipped", "image kind %s not handled" % kind, 0)


def reencode_jpeg(path, st, ctx):
    from PIL import Image
    tmp = make_tmp(path.parent, path.suffix.lower() or ".jpg")
    try:
        with Image.open(str(path)) as im:
            if im.mode not in ("RGB", "L", "CMYK"):
                im = im.convert("RGB")
            save_with_meta(im, tmp, "JPEG", quality=ctx["cfg"]["jpeg_quality"],
                           progressive=True, optimize=True)
        if ctx["cfg"]["exiftool"] and not exiftool_copy(path, tmp):
            tmp.unlink(missing_ok=True)
            return ("error", "exiftool metadata copy failed", 0)
        return finalize(ctx, path, st, tmp, path.suffix.lower(),
                        "jpeg q%d" % ctx["cfg"]["jpeg_quality"], None)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def reencode_webp(path, st, ctx):
    from PIL import Image
    tmp = make_tmp(path.parent, ".webp")
    try:
        with Image.open(str(path)) as im:
            save_with_meta(im, tmp, "WEBP", quality=ctx["cfg"]["jpeg_quality"], method=6)
        if ctx["cfg"]["exiftool"] and not exiftool_copy(path, tmp):
            tmp.unlink(missing_ok=True)
            return ("error", "exiftool metadata copy failed", 0)
        return finalize(ctx, path, st, tmp, ".webp",
                        "webp q%d" % ctx["cfg"]["jpeg_quality"], None)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def optimize_png(path, st, ctx):
    cfg = ctx["cfg"]
    if cfg["oxipng"]:
        tmp = make_tmp(path.parent, ".png")
        try:
            shutil.copyfile(str(path), str(tmp))
            r = subprocess.run([cfg["oxipng"], "-o", "6", str(tmp)],
                               capture_output=True, text=True, timeout=7200, check=False)
            if r.returncode == 0 and tmp.stat().st_size > 0:
                deep = pixel_compare(path, tmp) if cfg["deep_verify"] else None
                return finalize(ctx, path, st, tmp, path.suffix.lower(), "oxipng -o 6", deep)
            tmp.unlink(missing_ok=True)
        except Exception:
            tmp.unlink(missing_ok=True)
    from PIL import Image
    tmp = make_tmp(path.parent, ".png")
    try:
        with Image.open(str(path)) as im:
            save_with_meta(im, tmp, "PNG", optimize=True, compress_level=9)
        deep = pixel_compare(path, tmp) if cfg["deep_verify"] else None
        return finalize(ctx, path, st, tmp, path.suffix.lower(), "png zlib 9", deep)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def convert_raster(path, st, ctx, fmt, out_ext, label):
    from PIL import Image
    if not out_ext:
        out_ext = path.suffix.lower()
    tmp = make_tmp(path.parent, out_ext)
    try:
        with Image.open(str(path)) as im:
            if fmt == "PNG":
                save_with_meta(im, tmp, "PNG", optimize=True, compress_level=9)
            else:
                if im.mode not in ("RGB", "RGBA", "L"):
                    im = im.convert("RGB")
                save_with_meta(im, tmp, "TIFF", compression="tiff_lzw")
        if ctx["cfg"]["exiftool"] and not exiftool_copy(path, tmp):
            tmp.unlink(missing_ok=True)
            return ("error", "exiftool metadata copy failed", 0)
        deep = pixel_compare(path, tmp) if ctx["cfg"]["deep_verify"] else None
        return finalize(ctx, path, st, tmp, out_ext, label, deep)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def convert_avif(path, st, ctx):
    from PIL import Image
    tmp = make_tmp(path.parent, ".avif")
    try:
        with Image.open(str(path)) as im:
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGB")
            kwargs = {}
            exif = im.info.get("exif")
            icc = im.info.get("icc_profile")
            if exif:
                kwargs["exif"] = exif
            if icc:
                kwargs["icc_profile"] = icc
            try:
                im.save(str(tmp), "AVIF", quality=ctx["cfg"]["avif_quality"], **kwargs)
            except TypeError:
                im.save(str(tmp), "AVIF", quality=ctx["cfg"]["avif_quality"])
        if ctx["cfg"]["exiftool"] and not exiftool_copy(path, tmp):
            tmp.unlink(missing_ok=True)
            return ("error", "exiftool metadata copy failed", 0)
        return finalize(ctx, path, st, tmp, ".avif",
                        "avif q%d" % ctx["cfg"]["avif_quality"], None)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def transcode_jxl(path, st, ctx):
    cfg = ctx["cfg"]
    tmp = make_tmp(path.parent, ".jxl")
    try:
        r = subprocess.run([cfg["cjxl"], str(path), str(tmp)],
                           capture_output=True, text=True, timeout=7200, check=False)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return ("error", "cjxl failed: %s" % ((r.stderr or r.stdout or "")[-300:].strip()), 0)
        deep = None
        if cfg["deep_verify"] and cfg["djxl"]:
            deep = jxl_roundtrip(path, tmp, cfg["djxl"])
        return finalize(ctx, path, st, tmp, ".jxl", "jxl lossless jpeg transcode", deep)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def encode_animated_gif(path, st, ctx):
    if which("ffmpeg") is None:
        return ("error", "ffmpeg not available", 0)
    duration, _, _ = probe_video(path)
    tmp = make_tmp(path.parent, ".gif")
    slot = ctx["display"].claim_slot(path.name) if ctx["display"] else None
    try:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(path),
               "-vf", "split[s0][s1];[s0]palettegen=max_colors=255[p];[s1][p]paletteuse=dither=bayer:bayer_scale=5",
               "-loop", "0", str(tmp)]
        stop_event = ctx.get("stop_event")
        ok, err = run_ffmpeg_progress(
            cmd, duration, ctx["display"], slot, 3600,
            stop_event=stop_event)
        if not ok:
            tmp.unlink(missing_ok=True)
            if stop_event is not None and stop_event.is_set():
                return ("cancelled", "interrupted during GIF optimization", 0)
            return ("error", err or "ffmpeg failed on gif", 0)
        return finalize(ctx, path, st, tmp, ".gif", "gif palette optimize", None)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)
    finally:
        if ctx["display"] and slot is not None:
            ctx["display"].release_slot(slot)


def handle_video(path, st, ctx):
    cfg = ctx["cfg"]
    if not cfg["lossy"]:
        return ("skipped", "lossless-only mode leaves video untouched; drop --lossless-only to re-encode", 0)
    if which("ffmpeg") is None or which("ffprobe") is None:
        return ("error", "ffmpeg or ffprobe not available", 0)
    duration, pix_fmt, codec = probe_video(path)
    if not codec:
        return ("error", "no video stream or ffprobe failed", 0)
    if duration is not None and 1.0 <= duration <= 3.0:
        return ("skipped", "short clip %.1fs (possible motion or Live Photo component); deletion is permanently disabled, file left untouched" % duration, 0)
    if codec == cfg["codec"] and not cfg["force_reencode"]:
        return ("skipped", "already %s; set force_reencode true in config (or pass --force-reencode) to recompress" % cfg["codec"], 0)
    tmp = make_tmp(path.parent, ".mp4")
    slot = ctx["display"].claim_slot(path.name) if ctx["display"] else None
    try:
        cmd = build_video_cmd(cfg, path, tmp, pix_fmt)
        stop_event = ctx.get("stop_event")
        ok, err = run_ffmpeg_progress(
            cmd, duration, ctx["display"], slot, 21600,
            stop_event=stop_event)
        if not ok and cfg["gpu"] and not (
                stop_event is not None and stop_event.is_set()):
            cmd = build_video_cmd(cfg, path, tmp, pix_fmt, force_cpu=True)
            ok, err = run_ffmpeg_progress(
                cmd, duration, ctx["display"], slot, 21600,
                stop_event=stop_event)
        if not ok:
            tmp.unlink(missing_ok=True)
            if stop_event is not None and stop_event.is_set():
                return ("cancelled", "interrupted during video encoding", 0)
            return ("error", err or "ffmpeg failed", 0)
        out_duration, _, _ = probe_video(tmp)
        if out_duration is None:
            tmp.unlink(missing_ok=True)
            return ("error", "output failed ffprobe validation", 0)
        gate_ok, gate_note = vmaf_gate(
            cfg, path, tmp, duration, ctx.get("stop_event"))
        if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
            tmp.unlink(missing_ok=True)
            return ("cancelled", "interrupted during quality verification", 0)
        if not gate_ok:
            tmp.unlink(missing_ok=True)
            return ("error", "quality gate rejected output: %s" % gate_note, 0)
        if cfg["exiftool"] and not exiftool_copy(path, tmp):
            tmp.unlink(missing_ok=True)
            return ("error", "exiftool metadata copy failed", 0)
        return finalize(ctx, path, st, tmp, ".mp4",
                        "video %s%s" % (cfg["codec"], (" " + gate_note) if gate_note else ""), None)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)
    finally:
        if ctx["display"] and slot is not None:
            ctx["display"].release_slot(slot)


def audio_to_flac(path, st, ctx):
    if which("ffmpeg") is None:
        return ("error", "ffmpeg not available", 0)
    tmp = make_tmp(path.parent, ".flac")
    try:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(path),
               "-map", "0:a:0", "-map_metadata", "0", "-vn",
               "-c:a", "flac", "-compression_level", "8", str(tmp)]
        r = run_subprocess(
            cmd, timeout=7200, stop_event=ctx.get("stop_event"))
        if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
            tmp.unlink(missing_ok=True)
            return ("cancelled", "interrupted during audio encoding", 0)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return ("error", (r.stderr or "ffmpeg failed")[-300:].strip(), 0)
        if ctx["cfg"]["exiftool"] and not exiftool_copy(path, tmp):
            tmp.unlink(missing_ok=True)
            return ("error", "exiftool metadata copy failed", 0)
        deep = pcm_compare(path, tmp) if ctx["cfg"]["deep_verify"] else None
        return finalize(ctx, path, st, tmp, ".flac", "flac level 8 lossless", deep)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def audio_lossy(path, st, ctx):
    cfg = ctx["cfg"]
    if which("ffmpeg") is None:
        return ("error", "ffmpeg not available", 0)
    name = path.stem.lower()
    speech = any(word in name for word in ("recording", "call", "voice", "memo"))
    if cfg["audio_codec"] == "opus":
        out_ext = ".opus"
        args = ["-c:a", "libopus", "-b:a", "24k" if speech else cfg["audio_bitrate"]]
        if speech:
            args += ["-ac", "1"]
    else:
        out_ext = ".m4a"
        if speech:
            args = ["-c:a", "aac", "-b:a", "32k", "-ac", "1", "-ar", "16000"]
        else:
            args = ["-c:a", "aac", "-b:a", cfg["audio_bitrate"]]
    tmp = make_tmp(path.parent, out_ext)
    try:
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(path),
               "-map", "0:a:0", "-map_metadata", "0", "-vn"] + args
        if out_ext == ".m4a":
            cmd += ["-movflags", "+faststart"]
        cmd += [str(tmp)]
        r = run_subprocess(
            cmd, timeout=7200, stop_event=ctx.get("stop_event"))
        if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
            tmp.unlink(missing_ok=True)
            return ("cancelled", "interrupted during audio encoding", 0)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return ("error", (r.stderr or "ffmpeg failed")[-300:].strip(), 0)
        if cfg["exiftool"] and not exiftool_copy(path, tmp):
            tmp.unlink(missing_ok=True)
            return ("error", "exiftool metadata copy failed", 0)
        label = "audio %s%s" % (cfg["audio_codec"], " speech profile" if speech else "")
        return finalize(ctx, path, st, tmp, out_ext, label, None)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        return ("error", str(e), 0)


def handle_audio(path, st, ctx, kind):
    cfg = ctx["cfg"]
    if kind in ("wav", "aiff"):
        return audio_to_flac(path, st, ctx)
    if kind == "flac":
        return ("skipped", "already lossless (FLAC)", 0)
    if not cfg["lossy"]:
        return ("skipped", "lossy audio source; lossless-only mode leaves it untouched", 0)
    return audio_lossy(path, st, ctx)


def handle_pdf(path, st, ctx):
    cfg = ctx["cfg"]
    tmp_a = make_tmp(path.parent, ".pdf")
    tmp_b = make_tmp(path.parent, ".pdf")
    candidates = []
    labels = {}
    try:
        if cfg["pikepdf"]:
            try:
                import pikepdf
                with pikepdf.open(str(path)) as pdf:
                    pdf.save(str(tmp_a), object_stream_mode=pikepdf.ObjectStreamMode.generate,
                             recompress_flate=True, compress_streams=True)
                with pikepdf.open(str(tmp_a)) as chk:
                    if len(chk.pages) > 0 and tmp_a.stat().st_size > 0:
                        candidates.append(tmp_a)
                        labels[str(tmp_a)] = "pikepdf object streams"
            except Exception:
                tmp_a.unlink(missing_ok=True)
        if cfg["lossy"] and cfg["gs"]:
            try:
                r = subprocess.run(
                    [cfg["gs"], "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                     "-dPDFSETTINGS=%s" % cfg["pdf_settings"], "-dNOPAUSE", "-dQUIET", "-dBATCH",
                     "-dDetectDuplicateImages=true", "-dCompressFonts=true",
                     "-sOutputFile=%s" % tmp_b, str(path)],
                    capture_output=True, text=True, timeout=3600, check=False)
                if r.returncode == 0 and tmp_b.stat().st_size > 0:
                    candidates.append(tmp_b)
                    labels[str(tmp_b)] = "ghostscript %s" % cfg["pdf_settings"]
            except Exception:
                tmp_b.unlink(missing_ok=True)
        if not candidates:
            if not cfg["pikepdf"] and not (cfg["lossy"] and cfg["gs"]):
                return ("skipped", "no PDF engine available (install pikepdf or ghostscript)", 0)
            return ("error", "PDF engines produced no valid output", 0)
        best = min(candidates, key=lambda p: p.stat().st_size)
        for cand in candidates:
            if cand != best:
                cand.unlink(missing_ok=True)
        return finalize(ctx, path, st, best, ".pdf", labels.get(str(best), "pdf"), None)
    except Exception as e:
        for t in (tmp_a, tmp_b):
            t.unlink(missing_ok=True)
        return ("error", str(e), 0)


def compressibility_probe(path, level):
    name, mod = zstd_backend()
    if name is None:
        return False
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 20)
        if not data:
            return False
        out = zstd_compress_bytes(mod, name, data, level)
        return len(out) < len(data) * 0.99
    except Exception:
        return False


def handle_generic(path, st, ctx, kind):
    cfg = ctx["cfg"]
    if not cfg["generic_zstd"]:
        return ("skipped", "non-media file (%s); generic zstd not enabled" % kind, 0)
    if zstd_backend()[0] is None:
        return ("skipped", "zstd backend unavailable (Python 3.14+ or pip install zstandard)", 0)
    if not compressibility_probe(path, cfg["zstd_level"]):
        return ("incompressible", "sampled compression ratio below threshold; left untouched", 0)
    tmp = make_tmp(path.parent, ".zst")
    ok, err = zstd_compress_file(path, tmp, cfg["zstd_level"])
    if not ok:
        tmp.unlink(missing_ok=True)
        return ("error", "zstd failed: %s" % err, 0)
    deep = None
    if cfg["deep_verify"]:
        deep = (lambda s=path, d=tmp: zstd_verify_roundtrip(s, d))
    return finalize(ctx, path, st, tmp, ".zst", "zstd level %d" % cfg["zstd_level"], deep)


def process_file(path, base_ctx):
    stop_event = base_ctx.get("stop_event")
    display = base_ctx["display"]
    if stop_event is not None and stop_event.is_set():
        add_result("cancelled", path, "interrupted before processing", 0)
        if display:
            display.advance(0, 0, False, "cancelled  %s" % path.name)
        return
    manifest = base_ctx["manifest"]
    events = base_ctx["events"]
    st = safe_stat(path)
    if st is None:
        status, note, saved = "error", "file unreadable or vanished", 0
        add_result(status, path, note, saved)
        events.log(status, path=str(path), note=note)
        if display:
            display.advance(0, 0, True, "%s  %s" % (status, path.name))
        return
    fctx = dict(base_ctx)
    key = file_key(path, st)
    fctx["key"] = key
    if st.st_size == 0:
        status, note, saved = "skipped", "empty file", 0
        manifest.set_file(key, str(path), st.st_size, st.st_mtime, status, note, None, None, 0, None)
    else:
        prev = manifest.final_status(key)
        if prev is not None:
            status, note, saved = "resumed", "already %s (manifest)" % prev, 0
        elif getattr(st, "st_nlink", 1) > 1:
            status, note, saved = "skipped", "hard-linked file, left untouched", 0
            manifest.set_file(key, str(path), st.st_size, st.st_mtime, status, note, None, None, 0, None)
        else:
            kind = sniff_type(path)
            fam = family_of(kind)
            extra = ""
            ext_fam = family_of_ext(path.suffix)
            if ext_fam is not None and ext_fam != fam:
                extra = " [extension %s does not match detected %s]" % (path.suffix, kind)
            if fam == "image":
                status, note, saved = handle_image(path, st, fctx, kind)
            elif fam == "video":
                status, note, saved = handle_video(path, st, fctx)
            elif fam == "audio":
                status, note, saved = handle_audio(path, st, fctx, kind)
            elif fam == "pdf":
                status, note, saved = handle_pdf(path, st, fctx)
            elif fam == "archive":
                status, note, saved = "skipped", "archive or compressed container, left untouched", 0
            else:
                status, note, saved = handle_generic(path, st, fctx, kind)
            if status != "replaced" and status != "resumed":
                manifest.set_file(key, str(path), st.st_size, st.st_mtime, status, note, None, None, 0, None)
            if extra:
                note = note + extra
    add_result(status, path, note, saved)
    events.log(status, path=str(path), note=note, saved=saved)
    if display:
        display.advance(st.st_size, saved, status == "error", "%s  %s" % (status, path.name))


def main():
    global CFG
    ap = argparse.ArgumentParser(
        description="Bulk storage reduction with sensible defaults, a persistent JSON config, "
                    "consent-free lossy media compression guarded by a sampled VMAF quality gate, "
                    "atomic durable replacement, a crash-safe manifest, and live per-file progress bars")
    ap.add_argument("directory")
    ap.add_argument("--config", metavar="FILE",
                    help="load settings from this JSON file instead of the auto-discovered config")
    ap.add_argument("--save-config", action="store_true",
                    help="write the resolved settings to the config file so future runs use them")
    ap.add_argument("--lossless-only", action="store_true", default=None,
                    help="restrict to lossless operations; media re-encoding is disabled")
    ap.add_argument("--codec", choices=["auto", "av1", "hevc", "h264"], default=None)
    ap.add_argument("--av1-crf", type=int, default=None)
    ap.add_argument("--av1-preset", default=None)
    ap.add_argument("--crf", type=int, default=None, help="libx265 CRF")
    ap.add_argument("--x264-crf", type=int, default=None)
    ap.add_argument("--preset", default=None, help="libx265/x264 preset")
    ap.add_argument("--cq", type=int, default=None, help="NVENC cq")
    ap.add_argument("--nvenc-preset", default=None)
    ap.add_argument("--film-grain", type=int, default=None,
                    help="SVT-AV1 film grain synthesis level 1-15, 0 disables")
    ap.add_argument("--force-reencode", action="store_true", default=None,
                    help="re-encode videos already in the target codec")
    ap.add_argument("--cpu-video", action="store_true", default=None,
                    help="disable NVENC, use software encoding")
    ap.add_argument("--video-workers", type=int, default=None)
    ap.add_argument("--jpeg-quality", type=int, default=None)
    ap.add_argument("--avif", action="store_true", default=None,
                    help="convert JPEG/PNG/BMP/TIFF to AVIF")
    ap.add_argument("--avif-quality", type=int, default=None)
    ap.add_argument("--jxl", action="store_true", default=None,
                    help="losslessly transcode JPEG to JPEG XL when cjxl is available")
    ap.add_argument("--audio-codec", choices=["aac", "opus"], default=None)
    ap.add_argument("--audio-bitrate", default=None)
    ap.add_argument("--pdf-settings", choices=["/ebook", "/screen", "/printer"], default=None)
    ap.add_argument("--generic-zstd", action="store_true", default=None,
                    help="compress non-media files to .zst (format change, explicit opt-in)")
    ap.add_argument("--zstd-level", type=int, default=None)
    ap.add_argument("--deep-verify", action="store_true", default=None,
                    help="byte- or pixel-exact verification of lossless outputs before replacement")
    ap.add_argument("--vmaf-samples", type=int, default=None,
                    help="sampled VMAF quality gate for lossy video, 0 disables")
    ap.add_argument("--vmaf-threshold", type=float, default=None)
    ap.add_argument("--skip-dedup", action="store_true", default=None)
    ap.add_argument("--backup", metavar="DIR", default=None,
                    help="keep every replaced original under DIR, enabling restore_files.py --rollback")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    enable_ansi()
    root = Path(args.directory).resolve()
    if not root.is_dir():
        fatal("Directory not found: %s" % root)
    if find_spec("PIL") is None:
        fatal("Pillow is required: pip install -r requirements.txt")

    config = {}
    config_path_used = None
    if args.config:
        cfgp = Path(args.config)
        if cfgp.exists():
            config = load_config(cfgp)
            config_path_used = cfgp
        else:
            print("[warn] config file not found: %s" % cfgp)
    else:
        for p in config_search_paths():
            if p.exists():
                config = load_config(p)
                config_path_used = p
                break

    vals = resolve_settings(args, config)
    if args.limit is not None and args.limit < 1:
        fatal("--limit must be at least 1")

    if args.save_config:
        target = Path(args.config) if args.config else (Path(__file__).resolve().parent / "config.json")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(vals, indent=2) + "\n", encoding="utf-8")
            print("settings saved to %s; future runs will load them automatically" % target)
        except Exception as e:
            print("[warn] could not save config: %s" % e)

    lossy = not vals["lossless_only"]

    ffmpeg_ok = which("ffmpeg") is not None and which("ffprobe") is not None
    if not ffmpeg_ok:
        print("[warn] ffmpeg/ffprobe not found; video and audio paths will record errors, other types are unaffected")

    encoders = ffmpeg_encoders() if ffmpeg_ok else set()
    codec = vals["codec"]
    if codec == "auto":
        if not vals["cpu_video"] and ffmpeg_ok and nvenc_available("hevc_nvenc"):
            codec = "hevc"
        elif "libsvtav1" in encoders:
            codec = "av1"
        elif "libx265" in encoders:
            codec = "hevc"
        else:
            codec = "h264"
    if codec == "av1" and "libsvtav1" not in encoders:
        print("[warn] libsvtav1 not in this ffmpeg build; falling back to HEVC")
        codec = "hevc" if "libx265" in encoders else "h264"
    hw_name = {"hevc": "hevc_nvenc", "h264": "h264_nvenc", "av1": "av1_nvenc"}.get(codec)
    gpu = bool(hw_name) and not vals["cpu_video"] and ffmpeg_ok and nvenc_available(hw_name)

    avif_ok = pillow_avif_available()
    use_avif = vals["avif"] and lossy and avif_ok
    if vals["avif"] and not lossy:
        print("[warn] AVIF conversion is disabled under --lossless-only")
    if vals["avif"] and lossy and not avif_ok:
        print("[warn] Pillow AVIF support not available in this build; AVIF disabled")

    CFG = {
        "lossy": lossy,
        "codec": codec,
        "gpu": gpu,
        "crf": vals["crf"],
        "x264_crf": vals["x264_crf"],
        "preset": vals["preset"],
        "cq": vals["cq"],
        "nvenc_preset": vals["nvenc_preset"],
        "av1_crf": vals["av1_crf"],
        "av1_preset": vals["av1_preset"],
        "film_grain": max(0, vals["film_grain"]),
        "force_reencode": vals["force_reencode"],
        "jpeg_quality": vals["jpeg_quality"],
        "avif": use_avif,
        "avif_quality": vals["avif_quality"],
        "jxl": vals["jxl"],
        "cjxl": which("cjxl") if vals["jxl"] else None,
        "djxl": which("djxl"),
        "audio_codec": vals["audio_codec"],
        "audio_bitrate": vals["audio_bitrate"],
        "pdf_settings": vals["pdf_settings"],
        "generic_zstd": vals["generic_zstd"],
        "zstd_level": vals["zstd_level"],
        "deep_verify": vals["deep_verify"],
        "vmaf_samples": vals["vmaf_samples"],
        "vmaf_threshold": vals["vmaf_threshold"],
        "exiftool": which("exiftool"),
        "oxipng": which("oxipng"),
        "pikepdf": pikepdf_available(),
        "gs": gs_binary(),
    }
    if vals["jxl"] and CFG["cjxl"] is None:
        print("[warn] cjxl not found on PATH; JPEG XL transcoding disabled (install libjxl tools)")
    if CFG["exiftool"] is None:
        print("[warn] exiftool not found; media metadata relies on embedded Pillow tags only")
    if not CFG["pikepdf"]:
        print("[warn] pikepdf not installed; lossless PDF optimization unavailable")

    backup_root = None
    if vals["backup"]:
        backup_root = Path(str(vals["backup"])).expanduser().resolve()
        if backup_root == root or backup_root in root.parents or root in backup_root.parents:
            fatal("backup directory and target directory must not overlap")

    started = time.time()
    manifest = Manifest(root)
    events = EventLog(root)
    events.log("run_start", kind="compress", lossy=lossy, codec=codec, gpu=gpu)
    removed = sweep_temp_files(root)
    if removed:
        print("swept %d leftover temporary files from an interrupted run" % removed)

    extra_exclude = []
    if backup_root is not None and root in backup_root.parents:
        extra_exclude.append(backup_root)
    print("scanning %s ..." % root)
    files, symlink_count = scan_files(root, extra_exclude)

    if vals["skip_dedup"]:
        unique_files, dupes = files, []
    else:
        print("hashing for duplicate detection ...")
        unique_files, dupes = find_duplicates(files)
    for dup, original in dupes:
        st = safe_stat(dup)
        if st is None:
            continue
        manifest.set_file(file_key(dup, st), str(dup), st.st_size, st.st_mtime,
                          "duplicate", "identical bytes to %s" % original, None, None, 0, None)
        add_result("duplicate", dup, "identical bytes to %s" % original, 0)

    final_keys = manifest.final_keys()
    pending = []
    for f in unique_files:
        st = safe_stat(f)
        if st is None or st.st_size == 0:
            pending.append(f)
            continue
        if file_key(f, st) not in final_keys:
            pending.append(f)
    if args.limit:
        pending = pending[: args.limit]
    total_bytes = sum(safe_size(f) for f in pending)

    if gpu:
        encoder_desc = "%s preset %s cq %d (NVENC)" % (hw_name, vals["nvenc_preset"], vals["cq"])
    elif codec == "av1":
        encoder_desc = "libsvtav1 preset %s crf %d" % (vals["av1_preset"], vals["av1_crf"])
    elif codec == "hevc":
        encoder_desc = "libx265 crf %d preset %s" % (vals["crf"], vals["preset"])
    else:
        encoder_desc = "libx264 crf %d preset %s" % (vals["x264_crf"], vals["preset"])

    video_workers = vals["video_workers"] or (3 if gpu else max(1, min(2, CPU // 3)))
    fast_workers = max(2, CPU - video_workers)
    zname = zstd_backend()[0] or "none"
    if vals["vmaf_samples"] > 0:
        gate_desc = "VMAF %d sample%s at %.1f" % (vals["vmaf_samples"],
                                                  "s" if vals["vmaf_samples"] != 1 else "",
                                                  vals["vmaf_threshold"])
    else:
        gate_desc = "off"

    print("config             : %s" % (config_path_used if config_path_used
                                       else "none (defaults; use --save-config to persist)"))
    print("files discovered   : %d (%d symlinks skipped)" % (len(files), symlink_count))
    print("duplicates skipped : %d" % len(dupes))
    print("to process         : %d (%s)" % (len(pending), fmt_bytes(total_bytes)))
    print("mode               : %s" % ("lossless only" if vals["lossless_only"] else "lossy (default)"))
    print("video encoder      : %s" % encoder_desc)
    print("quality gate       : %s" % gate_desc)
    print("video workers      : %d   fast workers: %d" % (video_workers, fast_workers))
    print("tools              : exiftool=%s oxipng=%s cjxl=%s pikepdf=%s ghostscript=%s zstd=%s" % (
        "yes" if CFG["exiftool"] else "no",
        "yes" if CFG["oxipng"] else "no",
        "yes" if CFG["cjxl"] else "no",
        "yes" if CFG["pikepdf"] else "no",
        "yes" if CFG["gs"] else "no",
        zname))
    if codec != "av1" and "libsvtav1" in encoders:
        print("tip                : set codec to av1 in the config for higher compression at equal quality")
    if vals["backup"] is None:
        print("tip                : set backup in the config (or --backup DIR) to enable one-command rollback")
    print("policy             : short 1-3s clips are never deleted; originals only replaced when smaller")
    print()

    if not pending:
        print("nothing left to process")
        manifest.record_run("compress", started, time.time(), {"processed": 0})
        manifest.close()
        return

    display = ProgressDisplay(len(pending), total_bytes, video_workers,
                              tty=(not args.no_progress) and sys.stdout.isatty())
    stop_event = threading.Event()
    base_ctx = {
        "root": root,
        "stop_event": stop_event,
        "manifest": manifest,
        "events": events,
        "display": display,
        "cfg": CFG,
        "backup": backup_root,
    }

    videos = sorted((f for f in pending if family_of_ext(f.suffix) == "video"),
                    key=safe_size, reverse=True)
    rest = sorted((f for f in pending if family_of_ext(f.suffix) != "video"),
                  key=safe_size, reverse=True)

    video_pool = ThreadPoolExecutor(max_workers=video_workers, thread_name_prefix="video")
    fast_pool = ThreadPoolExecutor(max_workers=fast_workers, thread_name_prefix="fast")
    futures = {}
    for f in videos:
        futures[video_pool.submit(process_file, f, base_ctx)] = f
    for f in rest:
        futures[fast_pool.submit(process_file, f, base_ctx)] = f

    display.redraw(force=True)
    interrupted = False
    try:
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc is not None:
                add_result("error", futures[fut], "unhandled worker exception: %r" % exc, 0)
                if display:
                    display.advance(0, 0, True, "worker crash")
    except KeyboardInterrupt:
        interrupted = True
        stop_event.set()
        for future in futures:
            future.cancel()

    if interrupted:
        print("interrupt received; stopping active encodes and preserving unfinished inputs")
    fast_pool.shutdown(wait=True, cancel_futures=interrupted)
    video_pool.shutdown(wait=True, cancel_futures=interrupted)
    display.finish()

    finished = time.time()
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    print()
    print("=" * 78)
    print("COMPRESSION %s" % ("INTERRUPTED" if interrupted else "COMPLETE"))
    print("=" * 78)
    print("  results recorded  : %d" % len(results))
    for status in sorted(counts):
        print("  %-16s : %d" % (status, counts[status]))
    print("  bytes saved       : %s" % fmt_bytes(total_saved))
    print("  encoder           : %s" % encoder_desc)
    print("  elapsed           : %s" % fmt_seconds(finished - started))
    print("=" * 78)

    errors = [r for r in results if r["status"] == "error"]
    if errors:
        print("\nERRORS [%d]" % len(errors))
        for r in errors[:40]:
            print("  %s" % r["path"])
            print("      %s" % r["note"][:200])
        if len(errors) > 40:
            print("  ... and %d more in the JSON report" % (len(errors) - 40))

    report = {
        "kind": "compress",
        "root": str(root),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "interrupted": interrupted,
        "mode": "lossy" if lossy else "lossless",
        "encoder": encoder_desc,
        "codec": codec,
        "gpu": gpu,
        "settings": vals,
        "counts": counts,
        "total_saved_bytes": total_saved,
        "results": results,
    }
    report_path = root / STORE_DIR / ("report-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("\nreport   : %s" % report_path)
    except Exception as e:
        print("\n[warn] could not write report: %s" % e)
    print("manifest : %s" % (root / STORE_DIR / "manifest.db"))

    manifest.record_run("compress", started, finished, {"counts": counts, "saved": total_saved})
    manifest.close()
    events.log("run_end", kind="compress", interrupted=interrupted, saved=total_saved)


if __name__ == "__main__":
    main()
