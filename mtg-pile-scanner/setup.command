#!/bin/bash
# macOS / Linux setup. If macOS blocks it: right-click the file > Open, or run:  chmod +x setup.command
cd "$(dirname "$0")"
PY=""
for v in python3.12 python3.11 python3.13 python3.10 python3; do
  if command -v $v >/dev/null 2>&1 && $v -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then PY=$v; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ not found. Install it from https://www.python.org/downloads/ or run: brew install python@3.12"
  read -p "Press Enter to close"; exit 1
fi
echo "Using $($PY --version)"
rm -rf .venv
$PY -m venv .venv || { read -p "venv failed. Press Enter"; exit 1; }
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt || { echo "Install failed. Python 3.12 is the safest choice."; read -p "Press Enter"; exit 1; }
.venv/bin/python -c "import uvicorn, fastapi, cv2, rapidfuzz, openpyxl, ijson, onnxruntime, rapidocr" || { read -p "Import check failed. Press Enter"; exit 1; }
echo; echo "Setup complete. Double-click run.command to start the scanner."
read -p "Press Enter to close"
