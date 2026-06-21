@echo off
cd /d "%~dp0"

if /i "%~1"=="--mode" (
    python orchestrate.py %*
    goto :end
)

python -m uvicorn orchestration_server:app --host 127.0.0.1 --port 8765 --reload

:end
