@echo off
REM TBox server (Windows). Optional: pip install -r server\requirements.txt
cd /d "%~dp0server"
set TBOX_DATA_DIR=%~dp0data
python run_server.py
pause
