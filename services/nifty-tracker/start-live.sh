#!/bin/bash
# Start the tracker LIVE (needs a valid Dhan token (Dhan-Login)) — auto-stops 15:35.
cd "$(dirname "$0")" || exit 1
PP=$(lsof -tiTCP:8899 -sTCP:LISTEN 2>/dev/null); [ -n "$PP" ] && kill "$PP" 2>/dev/null && sleep 2
rm -f snapshot.json
source .venv/bin/activate
echo "Starting LIVE (headless)…"
# Order-flow engine (~/Documents/dhan/orderflow/of_engine.py): Dhan trades + 20-level + 200-level depth,
# walls / absorption / icebergs / footprint / heatmap. Replaces the older depth_recorder.py (keeps its file format).
DH="${MP_DHAN_DIR:-$HOME/Documents/dhan}"
if [ -x "$DH/venv/bin/python" ] && [ -f "$DH/orderflow/of_engine.py" ] && ! pgrep -f of_engine.py >/dev/null; then
  (cd "$DH/orderflow" && nohup caffeinate -i "$DH/venv/bin/python" -u of_engine.py >> of_engine.log 2>&1 &)
  echo "Order-flow engine started (trades + 20-level + 200-level depth)."
fi
RUN_MODE=live nohup python run.py --mode live --no-browser --exit-at 15:35 > tracker.log 2>&1 &
sleep 3; echo "running PID(s): $(pgrep -f run.py | tr '\n' ' ')"
echo "Open  http://<your-tailscale-ip>:8899   ·  logs: bash logs.sh"
