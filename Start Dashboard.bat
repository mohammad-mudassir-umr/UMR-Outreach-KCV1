@echo off
cd /d "%~dp0"
if not exist venv (
    echo First-time setup — installing dependencies, this takes a minute...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)
start "" http://localhost:5050
python dashboard.py
pause
