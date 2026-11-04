#!/bin/bash
# Start the tracker in SIM (no market data needed) — for testing remote access.
cd "$(dirname "$0")" || exit 1
PP=$(lsof -tiTCP:8899 -sTCP:LISTEN 2>/dev/null); [ -n "$PP" ] && kill "$PP" 2>/dev/null && sleep 2
pkill -f "run.py" 2>/dev/null; sleep 1; rm -f snapshot.json
source .venv/bin/activate
echo "Starting SIM (headless)…"
RUN_MODE=sim nohup python run.py --mode sim --no-browser > tracker.log 2>&1 &
sleep 3; echo "running PID(s): $(pgrep -f run.py | tr '\n' ' ')"
echo "Open  http://<your-tailscale-ip>:8899   ·  logs: bash logs.sh"
