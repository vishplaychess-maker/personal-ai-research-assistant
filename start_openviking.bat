@echo off
REM Start the OpenViking context-DB sidecar for Thunder AI (Phase 1 PoC).
REM All state stays on D: — config D:\openviking\ov.conf, data D:\openviking\data.
REM Requires: local Ollama running on 11434 (nomic-embed-text + llama3.2:1b).

if not exist "D:\openviking\venv\Scripts\openviking-server.exe" (
    echo OpenViking server not installed. Run:
    echo   python -m venv D:\openviking\venv ^&^& D:\openviking\venv\Scripts\pip install openviking
    exit /b 1
)

echo Starting OpenViking server on http://127.0.0.1:1933 ...
"D:\openviking\venv\Scripts\openviking-server.exe" --config "D:\openviking\ov.conf"
