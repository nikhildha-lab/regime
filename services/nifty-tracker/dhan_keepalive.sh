#!/bin/bash
# Keeps the 24h Dhan token alive: renews it every 4h OUTSIDE market hours
# (renewing mid-session could swap the token under the running feed).
cd "$(dirname "$0")" || exit 0
while true; do
  H=$(date +%H%M); DOW=$(date +%u)
  if [ -f .dhan_token ] && { [ "$DOW" -ge 6 ] || [ "$H" -lt 900 ] || [ "$H" -gt 1540 ]; }; then
    .venv/bin/python -m src.dhan_auth --renew >> dhan_keepalive.log 2>&1
  fi
  sleep 14400
done
