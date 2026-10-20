@echo off
REM Double-click: starts TBox (if needed) and opens it in its own window.
cd /d "%~dp0"
start "" pythonw launch_tbox.py
