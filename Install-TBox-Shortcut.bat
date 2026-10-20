@echo off
REM Creates a "TBox" icon on your Desktop (with the TBox logo) that opens TBox in its own window.
cd /d "%~dp0"
python launch_tbox.py --install-shortcut
pause
