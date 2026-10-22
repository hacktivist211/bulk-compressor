@echo off
REM --- Build MIDDLE//OUT Console for Windows (one-file .exe) ---
pip install -r requirements.txt pyinstaller
pyinstaller --noconsole --onefile --name MiddleOutConsole ^
  --add-data "engine_compress.py;." --add-data "bc_common.py;." ^
  --add-data "verify_backup.py;." --add-data "restore_files.py;." ^
  --add-data "term_progress.py;." --add-data "phone_compress.py;." ^
  middleout_console.py
echo.
echo Done: dist\MiddleOutConsole.exe
echo Engine deps must be installed for the engine to run: pip install pillow pikepdf zstandard
echo (plus ffmpeg + exiftool in PATH for video/metadata)
pause
