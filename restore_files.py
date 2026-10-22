import argparse
import shutil
import sys
from pathlib import Path

from bc_common import (
    STORE_DIR, QUARANTINE_DIR, Manifest, atomic_replace, make_tmp,
    preserve_stat, hash_file,
)

def prune_empty_dirs(base):
    if not base.is_dir():
        return
    for d in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass
    try:
        if not any(base.iterdir()):
            base.rmdir()
    except Exception:
        pass


def restore_quarantine(root, manifest, dry_run):
    rows = manifest.quarantine_rows(unrestored_only=True)
    if not rows:
        print("no quarantined files to restore")
        return 0, 0
    restored = 0
    missing = 0
    for orig_path, q_path, reason in rows:
        source = Path(q_path)
        target = Path(orig_path)
        root_resolved = root.resolve()
        try:
            target_resolved = target.resolve(strict=False)
            source_resolved = source.resolve(strict=False)
        except OSError:
            print("[invalid path] %s -> %s" % (source, target))
            missing += 1
            continue
        if root_resolved not in target_resolved.parents:
            print("[invalid target] outside restore root: %s" % target)
            missing += 1
            continue
        quarantine_root = (root / QUARANTINE_DIR).resolve()
        if source_resolved != quarantine_root and quarantine_root not in source_resolved.parents:
            print("[invalid source] outside quarantine: %s" % source)
            missing += 1
            continue
        if not source.exists():
            print("[missing] %s" % source)
            missing += 1
            continue
        if target.exists() or target.is_symlink():
            print("[collision] %s already exists; left untouched" % target)
            continue
        if dry_run:
            print("[plan] %s -> %s" % (source, target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        manifest.mark_quarantine_restored(orig_path)
        print("[restored] %s" % target)
        restored += 1
    if not dry_run:
        prune_empty_dirs(root / QUARANTINE_DIR)
    return restored, missing


def rollback_replacements(root, manifest, dry_run):
    rows = manifest.rollback_rows()
    if not rows:
        print("no backed-up originals to roll back (run compression with --backup DIR)")
        return 0, 0
    rolled = 0
    missing = 0
    orphan_dir = root / STORE_DIR / "orphaned"
    root_resolved = root.resolve()
    for key, path, original_size, out_path, backup_path, backup_hash in rows:
        backup = Path(backup_path)
        target = Path(path)
        try:
            target_resolved = target.resolve(strict=False)
        except OSError:
            print("[invalid target] %s" % target)
            missing += 1
            continue
        if root_resolved not in target_resolved.parents:
            print("[invalid target] outside restore root: %s" % target)
            missing += 1
            continue
        out = Path(out_path) if out_path else None
        if out is not None:
            try:
                out_resolved = out.resolve(strict=False)
            except OSError:
                print("[invalid output] %s" % out)
                missing += 1
                continue
            if root_resolved not in out_resolved.parents and out_resolved != root_resolved:
                print("[invalid output] outside restore root: %s" % out)
                missing += 1
                continue
        if not backup.exists():
            print("[missing backup] %s" % backup)
            missing += 1
            continue
        if not backup.is_file() or backup.stat().st_size != original_size:
            print("[invalid backup] size mismatch: %s" % backup)
            missing += 1
            continue
        if backup_hash:
            algo, digest = hash_file(backup)
            if digest is None or "%s:%s" % (algo, digest) != backup_hash:
                print("[invalid backup] hash mismatch: %s" % backup)
                missing += 1
                continue
        if dry_run:
            print("[plan] %s -> %s" % (backup, target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = make_tmp(target.parent, target.suffix or ".bin")
        try:
            shutil.copyfile(str(backup), str(tmp))
            if tmp.stat().st_size != original_size:
                raise OSError("backup copy size mismatch")
            preserve_stat(backup.stat(), tmp)
            atomic_replace(tmp, target)
        except Exception as e:
            tmp.unlink(missing_ok=True)
            print("[restore failed] %s: %s" % (target, e))
            missing += 1
            continue
        if out is not None:
            if out.exists() and out != target:
                orphan_dir.mkdir(parents=True, exist_ok=True)
                dest = orphan_dir / out.name
                n = 1
                while dest.exists():
                    dest = orphan_dir / ("%s_%d%s" % (out.stem, n, out.suffix))
                    n += 1
                try:
                    shutil.move(str(out), str(dest))
                except OSError as e:
                    print("[orphan move failed] %s: %s" % (out, e))
                    missing += 1
                    continue
        manifest.mark_rolled_back(key)
        print("[rolled back] %s" % target)
        rolled += 1
    return rolled, missing


def main():
    ap = argparse.ArgumentParser(
        description="Restore quarantined files to their original locations, or roll back "
                    "compressed files to their backed-up originals recorded in the manifest")
    ap.add_argument("directory")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the planned actions without modifying anything")
    ap.add_argument("--rollback", action="store_true",
                    help="restore originals from the --backup archive recorded during compression")
    args = ap.parse_args()

    root = Path(args.directory).resolve()
    if not root.is_dir():
        print("Directory not found: %s" % root, file=sys.stderr)
        sys.exit(1)

    manifest = Manifest(root)
    if args.rollback:
        rolled, missing = rollback_replacements(root, manifest, args.dry_run)
        print()
        print("rolled back : %d" % rolled)
        print("missing     : %d" % missing)
        if args.dry_run:
            print("dry run: nothing was modified")
    else:
        restored, missing = restore_quarantine(root, manifest, args.dry_run)
        print()
        print("restored : %d" % restored)
        print("missing  : %d" % missing)
        if args.dry_run:
            print("dry run: nothing was modified")
    manifest.close()


if __name__ == "__main__":
    main()
