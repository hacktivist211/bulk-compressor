import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from bc_common import (
    STORE_DIR, QUARANTINE_DIR, cpu_count, elevate_priority,
    fmt_seconds, which, safe_stat, safe_size, file_key,
    sniff_type, family_of, sweep_temp_files, scan_files, probe_video,
    zstd_backend, zstd_check, Manifest, EventLog, same_file_snapshot,
)
from term_progress import (
    ProgressDisplay, run_ffmpeg_progress, run_subprocess, enable_ansi,
)

CPU = cpu_count()

_lock = threading.Lock()
results: list[dict[str, Any]] = []


def verify_image(path):
    from PIL import Image
    with Image.open(str(path)) as im:
        width, height = im.size
        if width == 0 or height == 0:
            raise ValueError("zero dimensions %dx%d" % (width, height))
        im.load()
    return "decoded"


def verify_video(path, display, fast, stop_event=None):
    duration, _, codec = probe_video(path)
    if not codec:
        raise ValueError("no video stream or ffprobe failed")
    if fast:
        return "probed %.1fs" % (duration or 0.0)
    if which("ffmpeg") is None:
        raise ValueError("ffmpeg not available")
    slot = display.claim_slot(path.name) if display else None
    try:
        cmd = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"]
        ok, err = run_ffmpeg_progress(
            cmd, duration, display, slot, 14400, stop_event=stop_event)
        if not ok and stop_event is not None and stop_event.is_set():
            raise InterruptedError("verification interrupted")
        if not ok:
            raise ValueError(err or "decode failed")
        return "decoded %.1fs" % (duration or 0.0)
    finally:
        if display and slot is not None:
            display.release_slot(slot)


def verify_audio(path, stop_event=None):
    if which("ffmpeg") is None:
        raise ValueError("ffmpeg not available")
    try:
        r = run_subprocess(
            ["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
            timeout=7200, stop_event=stop_event)
    except InterruptedError:
        raise
    except subprocess.TimeoutExpired:
        raise ValueError("decode timed out")
    if r.returncode != 0:
        raise ValueError((r.stderr or "decode failed")[-300:].strip())
    return "decoded"


def verify_pdf(path, stop_event=None):
    try:
        import pikepdf
    except ImportError:
        pikepdf = None
    if pikepdf is not None:
        with pikepdf.open(str(path)) as pdf:
            if len(pdf.pages) == 0:
                raise ValueError("PDF has 0 pages")
        return "pikepdf ok"
    gs = "gswin64c" if sys.platform == "win32" else "gs"
    if which(gs) is None:
        return "no PDF engine available"
    try:
        r = run_subprocess(
            [gs, "-dNOPAUSE", "-dBATCH", "-sDEVICE=nullpage", "-dQUIET", str(path)],
            timeout=1800, stop_event=stop_event)
    except InterruptedError:
        raise
    except subprocess.TimeoutExpired:
        raise ValueError("ghostscript timed out")
    if r.returncode != 0:
        raise ValueError("ghostscript render failed: %s" % (r.stderr or "")[-200:].strip())
    return "ghostscript ok"


def quarantine_file(root, path):
    rel = path.relative_to(root)
    dest = root / QUARANTINE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    final = dest
    n = 1
    while final.exists() or final.is_symlink():
        final = dest.with_name("%s_%d%s" % (dest.stem, n, dest.suffix))
        n += 1
    shutil.move(str(path), str(final))
    return final


def verify_file(path, ctx):
    display = ctx["display"]
    if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
        with _lock:
            results.append({"verdict": "skip", "path": str(path),
                            "note": "cancelled by user", "quarantined": None})
        if display:
            display.advance(0, 0, False, "cancelled  %s" % path.name)
        return
    st = safe_stat(path)
    key = None
    if st is not None:
        key = file_key(path, st)
    if st is None:
        verdict, note = "fail", "unreadable"
    elif st.st_size == 0:
        verdict, note = "fail", "empty file"
    else:
        kind = ctx["kinds"].get(path) or sniff_type(path)
        fam = family_of(kind)
        verdict, note = "skip", "not verifiable (%s)" % kind
        try:
            if fam == "image":
                note = verify_image(path)
                verdict = "ok"
            elif fam == "video":
                note = verify_video(path, display, ctx["fast"], ctx.get("stop_event"))
                verdict = "ok"
            elif fam == "audio":
                note = verify_audio(path, ctx.get("stop_event"))
                verdict = "ok"
            elif fam == "pdf":
                note = verify_pdf(path, ctx.get("stop_event"))
                verdict = "skip" if note.startswith("no PDF engine") else "ok"
            elif kind == "zstd":
                if zstd_backend()[0] is None:
                    verdict, note = "skip", "zstd backend unavailable"
                else:
                    ok, err = zstd_check(path)
                    verdict = "ok" if ok else "fail"
                    note = "zstd stream decompressed" if ok else (err or "zstd stream corrupt")
        except Exception as e:
            if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
                verdict, note = "skip", "cancelled by user"
            else:
                verdict, note = "fail", str(e)
    q_path = None
    if ctx.get("stop_event") is not None and ctx["stop_event"].is_set():
        verdict, note = "skip", "cancelled by user"
    if verdict == "fail" and st is not None and not same_file_snapshot(path, st):
        verdict, note = "skip", "file changed during verification; not quarantined"
    if verdict == "fail" and ctx["quarantine"]:
        q_path = quarantine_file(ctx["root"], path)
        ctx["manifest"].add_quarantine(str(path), str(q_path), note)
        ctx["events"].log("quarantined", path=str(path), q_path=str(q_path), reason=note)
    if key:
        ctx["manifest"].set_verify(key, verdict == "ok", note)
    with _lock:
        results.append({"verdict": verdict, "path": str(path), "note": note,
                        "quarantined": str(q_path) if q_path else None})
    ctx["events"].log("verify_" + verdict, path=str(path), note=note)
    if display:
        display.advance(st.st_size if st else 0, 0, verdict == "fail",
                        "%s  %s" % (verdict.upper(), path.name))


def main():
    ap = argparse.ArgumentParser(
        description="Non-destructive verification: full decode of images, video, audio and PDFs, "
                    "zstd stream checks, optional quarantine (never deletion)")
    ap.add_argument("directory")
    ap.add_argument("--fast", action="store_true",
                    help="structural probe only, skip full decode")
    ap.add_argument("--quarantine", action="store_true",
                    help="move failing files into _Quarantine/ preserving their directory tree; "
                         "recorded in the manifest, restore with restore_files.py")
    ap.add_argument("--video-workers", type=int, default=2)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()
    if args.video_workers < 1:
        ap.error("--video-workers must be at least 1")
    if args.workers is not None and args.workers < 1:
        ap.error("--workers must be at least 1")

    enable_ansi()
    elevate_priority()
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print("Directory not found: %s" % root, file=sys.stderr)
        sys.exit(1)

    manifest = Manifest(root)
    events = EventLog(root)
    started = time.time()
    events.log("run_start", kind="verify", fast=args.fast, quarantine=args.quarantine)
    removed = sweep_temp_files(root)
    if removed:
        print("swept %d leftover temporary files" % removed)

    files, symlinks = scan_files(root)
    kinds = {}
    for f in files:
        kinds[f] = sniff_type(f)

    video_workers = max(1, args.video_workers)
    fast_workers = args.workers or max(2, CPU - video_workers)

    print("verifying %s" % root)
    print("files: %d (%d symlinks skipped)   video workers: %d   fast workers: %d" % (
        len(files), symlinks, video_workers, fast_workers))
    print("mode: %s%s" % ("fast probe" if args.fast else "full decode",
                          "   quarantine ON" if args.quarantine else ""))
    print()

    display = ProgressDisplay(len(files), sum(safe_size(f) for f in files), video_workers,
                              tty=(not args.no_progress) and sys.stdout.isatty())
    stop_event = threading.Event()
    ctx = {"root": root, "manifest": manifest, "events": events, "display": display,
           "fast": args.fast, "quarantine": args.quarantine, "kinds": kinds,
           "stop_event": stop_event}

    videos = sorted((f for f in files if family_of(kinds[f]) == "video"),
                    key=safe_size, reverse=True)
    rest = [f for f in files if family_of(kinds[f]) != "video"]

    video_pool = ThreadPoolExecutor(max_workers=video_workers, thread_name_prefix="vvideo")
    fast_pool = ThreadPoolExecutor(max_workers=fast_workers, thread_name_prefix="vfast")
    futures = {}
    for f in videos:
        futures[video_pool.submit(verify_file, f, ctx)] = f
    for f in rest:
        futures[fast_pool.submit(verify_file, f, ctx)] = f

    display.redraw(force=True)
    interrupted = False
    try:
        for fut in as_completed(futures):
            exc = fut.exception()
            if exc is not None:
                with _lock:
                    results.append({"verdict": "fail", "path": str(futures[fut]),
                                    "note": "unhandled worker exception: %r" % exc,
                                    "quarantined": None})
                if display:
                    display.advance(0, 0, True, "worker crash")
    except KeyboardInterrupt:
        interrupted = True
        stop_event.set()
        for future in futures:
            future.cancel()

    if interrupted:
        print("interrupt received; stopping active verification safely")
    fast_pool.shutdown(wait=True, cancel_futures=interrupted)
    video_pool.shutdown(wait=True, cancel_futures=interrupted)
    display.finish()

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    quarantined = [r for r in results if r["quarantined"]]
    fails = [r for r in results if r["verdict"] == "fail"]

    print()
    print("=" * 78)
    print("VERIFICATION %s" % ("INTERRUPTED" if interrupted else "COMPLETE"))
    print("=" * 78)
    print("  files        : %d" % len(files))
    print("  passed       : %d" % counts.get("ok", 0))
    print("  failed       : %d" % counts.get("fail", 0))
    print("  skipped      : %d" % counts.get("skip", 0))
    print("  quarantined  : %d" % len(quarantined))
    print("  elapsed      : %s" % fmt_seconds(time.time() - started))
    print("=" * 78)

    if fails:
        print("\nFAILED [%d]" % len(fails))
        for r in fails[:50]:
            print("  %s" % r["path"])
            print("      %s" % r["note"][:200])
        if len(fails) > 50:
            print("  ... and %d more in the JSON report" % (len(fails) - 50))
    if fails and not args.quarantine:
        print("\nnote: rerun with --quarantine to move failing files into %s (never deleted)" % QUARANTINE_DIR)
        print("note: restore them any time with: python restore_files.py \"%s\"" % root)

    report = {
        "kind": "verify",
        "root": str(root),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "interrupted": interrupted,
        "fast": args.fast,
        "counts": counts,
        "results": results,
    }
    report_path = root / STORE_DIR / ("verification-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("\nreport   : %s" % report_path)
    except Exception as e:
        print("\n[warn] could not write report: %s" % e)

    manifest.record_run("verify", started, time.time(), {
        "ok": counts.get("ok", 0), "fail": counts.get("fail", 0),
        "skip": counts.get("skip", 0), "quarantined": len(quarantined)})
    manifest.close()
    events.log("run_end", kind="verify", interrupted=interrupted)


if __name__ == "__main__":
    main()
