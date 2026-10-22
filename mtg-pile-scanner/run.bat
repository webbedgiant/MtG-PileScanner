@echo off
cd /d %~dp0
if not exist .venv\Scripts\python.exe (echo Run setup.bat first. & pause & exit /b 1)
.venv\Scripts\python -c "import uvicorn, rapidocr" 2>nul || (echo Install is incomplete. Run setup.bat again. & pause & exit /b 1)
.venv\Scripts\python -m pip install truststore -q 2>nul
.venv\Scripts\python launcher.py
pause
