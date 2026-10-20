@echo off
REM TBox Edge Gateway (Windows). Copy gateway\gateway.example.json to gateway\gateway.json and edit it first.
REM For Modbus RTU / RS-485: pip install -r gateway\requirements.txt
cd /d "%~dp0gateway"
python run_gateway.py --config gateway.json
pause
