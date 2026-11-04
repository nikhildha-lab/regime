#!/bin/bash
# Market Pulse - start the live dashboard (macOS / Linux).
# First start: creates .venv and installs pandas, numpy, curl_cffi (needs internet, ~1 minute).
cd "$(dirname "$0")" || exit 1
# load .env (paths to the helper services, TZ) if present
set -a; [ -f .env ] && . ./.env; set +a
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. On a Mac run:  xcode-select --install   then start this again."; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First start: setting up (one time)..."
  python3 -m venv .venv || exit 1
  .venv/bin/pip install --quiet --upgrade pip
fi
if [ ! -f .venv/.req_ok ] || [ app/requirements.txt -nt .venv/.req_ok ]; then
  echo "Installing / updating packages..."
  .venv/bin/pip install --quiet -r app/requirements.txt || exit 1
  touch .venv/.req_ok
fi
# keep a Mac awake (not the screen) while the platform runs, so updates don't pause when it would idle-sleep
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i .venv/bin/python app/server.py "$@"
fi
exec .venv/bin/python app/server.py "$@"
