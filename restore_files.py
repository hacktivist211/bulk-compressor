import os
import sys
import shutil
import re
from pathlib import Path

def restore_files(target_dir):
    root = Path(target_dir).resolve()
    report_path = root / 'corrupt_files_report.txt'
    corrupt_dir = root / '_Corrupted_Files'

    if not report_path.exists():
        print(f"[ERROR] Found no tracking log at: {report_path}")
        print("Cannot restore without the original path log.")
        return

    if not corrupt_dir.is_dir():
        print(f"[ERROR] Quarantine folder missing or empty: {corrupt_dir}")
        return

    print(f"Reading quarantine logs from: {report_path}")
    
    with open(report_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Regex to extract the original absolute paths from the quarantine section of the report
    # Looks for lines starting with "Original Path: "
    original_paths = re.findall(r'Original Path:\s*(.*)', content)

    if not original_paths:
        print("[WARN] No quarantined file paths found in the report.")
        return

    print(f"Found {len(original_paths)} logged files to restore.\n")
    restored_count = 0
    missing_count = 0

    for path_str in original_paths:
        orig_path = Path(path_str.strip())
        
        # Look for the file inside the _Corrupted_Files folder
        # Checking for the exact name first
        quarantined_file = corrupt_dir / orig_path.name
        
        # If it's not found under the exact name, it might have been renamed due to a collision
        if not quarantined_file.exists():
            # Fallback search if the file was appended with an index like _0, _1
            matching_files = list(corrupt_dir.glob(f"{orig_path.stem}_*{orig_path.suffix}"))
            if matching_files:
                quarantined_file = matching_files[0] # Pick the collision match
            else:
                print(f"[MISSING] Could not find '{orig_path.name}' in quarantine folder. Skipping.")
                missing_count += 1
                continue

        # Recreate the original destination directory path if it was deleted
        orig_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.move(str(quarantined_file), str(orig_path))
            print(f"[RESTORED] -> {orig_path}")
            restored_count += 1
        except Exception as e:
            print(f"[ERROR] Failed to move {quarantined_file.name} back: {e}")

    print("\n" + "="*80)
    print("RESTORATION RUN COMPLETE")
    print("="*80)
    print(f"  Successfully Restored : {restored_count}")
    print(f"  Files Not Found       : {missing_count}")
    print("=" * 80)

    # Clean up the quarantine directory if it's completely empty now
    try:
        if corrupt_dir.exists() and not any(corrupt_dir.iterdir()):
            corrupt_dir.rmdir()
            print("\n✨ Cleaned up empty '_Corrupted_Files' directory.")
    except Exception as e:
        print(f"\n[NOTE] Could not auto-remove directory frame structure: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python restore_files.py \"D:\\Samsung\"")
    else:
        restore_files(sys.argv[1])