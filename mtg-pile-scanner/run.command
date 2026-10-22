#!/bin/bash
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "Run setup.command first."; read -p "Press Enter"; exit 1; }
.venv/bin/python -c "import uvicorn, rapidocr" 2>/dev/null || { echo "Install incomplete. Run setup.command again."; read -p "Press Enter"; exit 1; }
.venv/bin/python -m pip install truststore -q 2>/dev/null
.venv/bin/python launcher.py
