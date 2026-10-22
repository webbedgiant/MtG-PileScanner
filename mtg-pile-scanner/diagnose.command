#!/bin/bash
cd "$(dirname "$0")"
.venv/bin/python -m pip install truststore -q 2>/dev/null
.venv/bin/python diagnose.py
read -p "Press Enter to close"
