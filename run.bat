@echo off
REM Windows 开发模式一键
cd /d %~dp0
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt -r requirements-build.txt
python models/download_models.py || REM 模型已存在则跳过
python app.py
