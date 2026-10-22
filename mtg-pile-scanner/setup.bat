@echo off
cd /d %~dp0
set PYCMD=
for %%V in (3.12 3.11 3.13 3.10) do (
  if not defined PYCMD (
    py -%%V --version >nul 2>nul && set PYCMD=py -%%V
  )
)
if not defined PYCMD (
  python --version >nul 2>nul && set PYCMD=python
)
if not defined PYCMD (
  echo Python not found. Install Python 3.12 from https://www.python.org/downloads/ and tick "Add to PATH".
  pause & exit /b 1
)
echo Using: %PYCMD%
%PYCMD% --version
if exist .venv rmdir /s /q .venv
%PYCMD% -m venv .venv || (pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt || goto :fail
.venv\Scripts\python -c "import uvicorn, fastapi, cv2, rapidfuzz, openpyxl, ijson, onnxruntime, rapidocr" || goto :fail
echo.
echo Setup complete. Double-click run.bat to start the scanner.
pause & exit /b 0
:fail
echo.
echo Install failed. If your Python is very new (3.14+), install Python 3.12 from python.org and run setup.bat again.
pause & exit /b 1
