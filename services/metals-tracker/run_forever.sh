#!/bin/bash
# 24/7 supervisor for the Gold & Silver tracker (clone of crypto-tracker). Restarts the app if it exits.
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; else source "$HOME/Documents/crypto-tracker/.venv/bin/activate"; fi   # own venv, else the old shared Mac env
echo "[supervisor] started $(date)" >> tracker.log
while true; do
  CAF=""; command -v caffeinate >/dev/null && CAF="caffeinate -s"
  RUN_MODE=live PYTHONUNBUFFERED=1 $CAF python run_metals.py --mode live --no-browser >> tracker.log 2>&1
  echo "[supervisor] app exited $(date) - restarting in 10s" >> tracker.log
  sleep 10
done
