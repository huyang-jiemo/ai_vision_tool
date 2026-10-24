#!/bin/bash
# macOS 开发模式一键：建 venv -> 装依赖 -> 跑 app.py
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt -r requirements-build.txt
python models/download_models.py || true
python app.py
