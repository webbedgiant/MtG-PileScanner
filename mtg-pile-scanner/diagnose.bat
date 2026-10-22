@echo off
cd /d %~dp0
.venv\Scripts\python -m pip install truststore -q
.venv\Scripts\python diagnose.py
pause
