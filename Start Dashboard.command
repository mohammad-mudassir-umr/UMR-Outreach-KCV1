#!/bin/bash
cd "$(dirname "$0")"
if [ ! -d venv ]; then
    echo "First-time setup — installing dependencies, this takes a minute..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi
( sleep 2 && open http://localhost:5050 2>/dev/null || xdg-open http://localhost:5050 2>/dev/null ) &
python3 dashboard.py
