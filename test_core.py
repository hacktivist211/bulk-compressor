import argparse
import json
import os
import stat
import threading
import time
from pathlib import Path

import engine_compress
import phone_compress
from bc_common import (
    EventLog,
    Manifest,
    atomic_replace,
    file_key,
    make_tmp,
    same_file_snapshot,
    scan_files,
    sniff_type,
)
from restore_files import rollback_replacements
from term_progress import run_subprocess


def make_context(root, backup=None, stop_event=None):
    manifest = Manifest(root)
    events = EventLog(root)
    ctx = {
        "root": root,
        "backup": backup,
        "manifest": manifest,
        "events": events,
        "key": None,
    }
    if stop_event is not None:
        ctx["stop_event"] = stop_event
    return ctx, manifest


def test_sniff_type_recognizes_common_signatures(tmp_path):
    source = tmp_path / "renamed.bin"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"payload")
    assert sniff_type(source) == "png"

    source.write_bytes(b"%PDF-1.7\n")
    assert sniff_type(source) == "pdf"

    source.write_bytes(b"")
    assert sniff_type(source) == "empty"


def test_scan_excludes_store_quarantine_hidden_and_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "visible.txt").write_text("visible", encoding="utf-8")
    (root / ".hidden.txt").write_text("hidden", encoding="utf-8")
    store = root / ".bulk_compressor"
    store.mkdir()
    (store / "manifest.db").write_text("state", encoding="utf-8")
    quarantine = root / "_Quarantine"
    quarantine.mkdir()
    (quarantine / "bad.bin").write_bytes(b"bad")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "linked.txt").symlink_to(outside)
    except OSError:
        pass
    files, symlinks = scan_files(root)
    assert [p.name for p in files] == ["visible.txt"]
    assert symlinks in (0, 1)


def test_snapshot_detects_changed_source(tmp_path):
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    initial = source.stat()
    assert same_file_snapshot(source, initial)
    source.write_bytes(b"modified")
    os.utime(source, ns=(initial.st_atime_ns, initial.st_mtime_ns + 2_000_000))
    assert not same_file_snapshot(source, initial)


def test_finalize_replaces_only_smaller_output_and_preserves_readonly_mode(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    source = root / "asset.bin"
    source.write_bytes(b"A" * 256)
    source.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    initial = source.stat()
    temporary = make_tmp(root, ".bin")
    temporary.write_bytes(b"B" * 24)
    ctx, manifest = make_context(root)
    ctx["key"] = file_key(source, initial)
    try:
        status, note, saved = engine_compress.finalize(
            ctx, source, initial, temporary, ".bin", "test")
        assert status == "replaced"
        assert saved == 232
        assert source.read_bytes() == b"B" * 24
        assert stat.S_IMODE(source.stat().st_mode) == stat.S_IMODE(initial.st_mode)
        assert manifest.final_status(file_key(source, initial)) == "replaced"
    finally:
        manifest.close()


def test_finalize_keeps_original_when_source_changes(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    source = root / "asset.bin"
    source.write_bytes(b"original-content")
    initial = source.stat()
    temporary = make_tmp(root, ".bin")
    temporary.write_bytes(b"tiny")
    source.write_bytes(b"new-content-value")
    os.utime(source, ns=(initial.st_atime_ns, initial.st_mtime_ns + 5_000_000))
    ctx, manifest = make_context(root)
    try:
        status, note, saved = engine_compress.finalize(
            ctx, source, initial, temporary, ".bin", "test")
        assert status == "error"
        assert "source changed" in note
        assert source.read_bytes() == b"new-content-value"
        assert not temporary.exists()
    finally:
        manifest.close()


def test_finalize_backup_hash_and_rollback_roundtrip(tmp_path):
    root = tmp_path / "target"
    backup_root = tmp_path / "backup"
    root.mkdir()
    source = root / "asset.bin"
    original = bytes(range(256)) * 8
    source.write_bytes(original)
    initial = source.stat()
    temporary = make_tmp(root, ".bin")
    temporary.write_bytes(b"compressed")
    ctx, manifest = make_context(root, backup_root)
    ctx["key"] = file_key(source, initial)
    try:
        status, _, _ = engine_compress.finalize(
            ctx, source, initial, temporary, ".bin", "test")
        assert status == "replaced"
        rows = manifest.rollback_rows()
        assert len(rows) == 1
        assert rows[0][5]
        backup = Path(rows[0][4])
        assert backup.read_bytes() == original
        assert rollback_replacements(root, manifest, False) == (1, 0)
        assert source.read_bytes() == original
        assert manifest.final_status(file_key(source, initial)) == "rolled_back"
    finally:
        manifest.close()


def test_rollback_refuses_corrupt_backup(tmp_path):
    root = tmp_path / "target"
    backup_root = tmp_path / "backup"
    root.mkdir()
    source = root / "asset.bin"
    original = b"original-content" * 16
    source.write_bytes(original)
    initial = source.stat()
    temporary = make_tmp(root, ".bin")
    temporary.write_bytes(b"compressed")
    ctx, manifest = make_context(root, backup_root)
    ctx["key"] = file_key(source, initial)
    try:
        assert engine_compress.finalize(ctx, source, initial, temporary, ".bin", "test")[0] == "replaced"
        backup = Path(manifest.rollback_rows()[0][4])
        backup.write_bytes(b"X" * len(original))
        assert rollback_replacements(root, manifest, False) == (0, 1)
        assert source.read_bytes() == b"compressed"
    finally:
        manifest.close()


def test_manifest_migrates_old_files_table(tmp_path):
    import sqlite3

    root = tmp_path / "target"
    store = root / ".bulk_compressor"
    store.mkdir(parents=True)
    connection = sqlite3.connect(store / "manifest.db")
    connection.execute(
        "CREATE TABLE files (key TEXT PRIMARY KEY, path TEXT, size INTEGER, "
        "mtime REAL, status TEXT, reason TEXT, out_path TEXT, out_size INTEGER, "
        "bytes_saved INTEGER, backup_path TEXT, ts REAL)"
    )
    connection.commit()
    connection.close()
    manifest = Manifest(root)
    try:
        columns = {row[1] for row in manifest.conn.execute("PRAGMA table_info(files)")}
        assert "backup_hash" in columns
    finally:
        manifest.close()


def test_progress_subprocess_capture_and_cancellation():
    completed = run_subprocess(
        [os.sys.executable, "-c", "print('ok')"], timeout=2)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"

    stop = threading.Event()
    result = {}

    def run_long():
        try:
            run_subprocess(
                [os.sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=20, stop_event=stop)
        except InterruptedError:
            result["cancelled"] = True

    worker = threading.Thread(target=run_long)
    worker.start()
    time.sleep(0.35)
    stop.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result.get("cancelled") is True


def test_phone_recovery_does_not_trust_size_alone(tmp_path, capsys):
    work = tmp_path / "work"
    job = work / "job_partial"
    phone = tmp_path / "phone" / "DCIM"
    job.mkdir(parents=True)
    phone.mkdir(parents=True)
    source = phone / "clip.mp4"
    original = b"original-bytes" * 32
    output = b"compressed" * 4
    (job / "orig.mp4").write_bytes(original)
    source.write_bytes(b"partial-output")
    journal = {
        "src": str(source),
        "dest": str(source),
        "old": len(original),
        "new": len(output),
        "old_sha256": phone_compress.sha256_of(job / "orig.mp4"),
        "new_sha256": __import__("hashlib").sha256(output).hexdigest(),
    }
    (job / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
    phone_compress.recover_leftovers(work)
    assert source.read_bytes() == original
    assert not job.exists()
    assert "restored original" in capsys.readouterr().out


def test_phone_recovery_only_cleans_verified_completed_output(tmp_path):
    work = tmp_path / "work"
    job = work / "job_complete"
    phone = tmp_path / "phone" / "DCIM"
    job.mkdir(parents=True)
    phone.mkdir(parents=True)
    source = phone / "clip.3gp"
    destination = phone / "clip.mp4"
    original = b"original-bytes" * 32
    output = b"compressed-output"
    (job / "orig.3gp").write_bytes(original)
    destination.write_bytes(output)
    journal = {
        "src": str(source),
        "dest": str(destination),
        "old": len(original),
        "new": len(output),
        "old_sha256": phone_compress.sha256_of(job / "orig.3gp"),
        "new_sha256": __import__("hashlib").sha256(output).hexdigest(),
    }
    (job / "journal.json").write_text(json.dumps(journal), encoding="utf-8")
    phone_compress.recover_leftovers(work)
    assert destination.read_bytes() == output
    assert not job.exists()


def test_settings_reject_out_of_range_values():
    resolved = engine_compress.resolve_settings(
        argparse.Namespace(crf=-1, jpeg_quality=1000, vmaf_threshold=float("nan")), {})
    assert resolved["crf"] == engine_compress.DEFAULTS["crf"]
    assert resolved["jpeg_quality"] == engine_compress.DEFAULTS["jpeg_quality"]
    assert resolved["vmaf_threshold"] == engine_compress.DEFAULTS["vmaf_threshold"]


def test_finalize_serializes_colliding_output_paths(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    first = root / "same.jpg"
    second = root / "same.png"
    first.write_bytes(b"A" * 200)
    second.write_bytes(b"B" * 220)
    ctx, manifest = make_context(root)

    def replace_one(source, encoded):
        original_stat = source.stat()
        temporary = make_tmp(root, ".avif")
        temporary.write_bytes(encoded)
        local_ctx = dict(ctx)
        local_ctx["key"] = file_key(source, original_stat)
        return engine_compress.finalize(
            local_ctx, source, original_stat, temporary, ".avif", "test")[0]

    try:
        with __import__("concurrent.futures").futures.ThreadPoolExecutor(2) as pool:
            statuses = list(pool.map(
                lambda item: replace_one(*item),
                ((first, b"one"), (second, b"two"))))
        assert sorted(statuses) == ["error", "replaced"]
        assert (root / "same.avif").exists()
        assert sum(path.exists() for path in (first, second)) == 1
    finally:
        manifest.close()


def test_phone_target_resolution_deduplicates_nested_directories(tmp_path):
    mount = tmp_path / "mount"
    parent = mount / "Internal storage" / "DCIM"
    child = parent / "Camera"
    child.mkdir(parents=True)
    args = argparse.Namespace(
        mount_root=str(mount), device=None, storage=None)
    resolved_mount, targets = phone_compress.resolve_targets(
        [str(parent), str(child), str(parent)], args)
    assert resolved_mount == mount.resolve()
    assert targets == [parent.resolve()]


def test_verifier_does_not_quarantine_a_file_changed_during_verification(
        tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    source = root / "bad.jpg"
    source.write_bytes(b"initial")
    manifest = Manifest(root)
    events = EventLog(root)
    ctx = {
        "root": root,
        "display": None,
        "fast": False,
        "quarantine": True,
        "kinds": {source: "jpeg"},
        "manifest": manifest,
        "events": events,
        "stop_event": threading.Event(),
    }

    def change_then_fail(path):
        path.write_bytes(b"changed")
        raise ValueError("decode failed")

    monkeypatch.setattr("verify_backup.verify_image", change_then_fail)
    try:
        import verify_backup

        verify_backup.results.clear()
        verify_backup.verify_file(source, ctx)
        assert source.exists()
        assert not (root / "_Quarantine").exists()
        assert verify_backup.results[-1]["verdict"] == "skip"
    finally:
        manifest.close()


def test_phone_recovery_keeps_legacy_journal_without_hashes(tmp_path, capsys):
    work = tmp_path / "work"
    job = work / "job_legacy"
    phone = tmp_path / "phone" / "DCIM"
    job.mkdir(parents=True)
    phone.mkdir(parents=True)
    source = phone / "clip.mp4"
    original = b"same-sized-unverifiable-content"
    source.write_bytes(original)
    (job / "orig.mp4").write_bytes(original)
    (job / "journal.json").write_text(json.dumps({
        "src": str(source),
        "dest": str(source),
        "old": len(original),
        "new": len(original) - 1,
    }), encoding="utf-8")
    phone_compress.recover_leftovers(work)
    assert source.read_bytes() == original
    assert job.exists()
    assert "cannot prove recovery copy integrity" in capsys.readouterr().out


def test_phone_recovery_preserves_original_without_journal(tmp_path, capsys):
    work = tmp_path / "work"
    job = work / "job_interrupted"
    job.mkdir(parents=True)
    original = job / "orig.jpg"
    original.write_bytes(b"staged original")
    phone_compress.recover_leftovers(work)
    assert job.is_dir()
    assert original.read_bytes() == b"staged original"
    assert "preserved staged original" in capsys.readouterr().out
