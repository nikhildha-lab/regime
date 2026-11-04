#!/bin/bash
# Desk supervisor (portable version of the Mac "Start Trading Desk.command").
# Every 60 s it starts whatever is not running:
#   Market Pulse (:8050), the Dhan token keep-alive, and on weekdays 09:00-15:25 IST
#   the Nifty tracker (:8899) + Dhan order-flow engine. Leave it running (tmux / systemd).
# Paper / indications only - nothing here places orders.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
set -a; [ -f "$REPO/.env" ] && . "$REPO/.env"; set +a
TRACKER="${MP_NIFTY_TRACKER_DIR:-$REPO/services/nifty-tracker}"
LOG="$REPO/logs"; mkdir -p "$LOG"
up() { (command -v lsof >/dev/null && lsof -tiTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1) || (command -v ss >/dev/null && ss -ltn "sport = :$1" | grep -q LISTEN); }
say() { echo "[$(TZ=Asia/Kolkata date '+%d %b %H:%M')] $*"; }
command -v caffeinate >/dev/null && { caffeinate -i -w $$ & }
say "Desk supervisor started."
while true; do
  if ! up 8050; then
    say "starting Market Pulse"
    (cd "$REPO" && nohup ./run_market_pulse.sh --no-browser >> "$LOG/market_pulse.log" 2>&1 &)
  fi
  if [ -f "$TRACKER/.dhan_token" ] && ! pgrep -f dhan_keepalive.sh >/dev/null; then
    say "starting Dhan keep-alive"
    (nohup bash "$TRACKER/dhan_keepalive.sh" >/dev/null 2>&1 &)
  fi
  DOW=$(TZ=Asia/Kolkata date +%u); HM=$(TZ=Asia/Kolkata date +%H%M)
  if [ "$DOW" -le 5 ] && [ "$HM" -ge 900 ] && [ "$HM" -lt 1525 ] && ! up 8899; then
    say "starting Nifty tracker + order-flow engine"
    bash "$TRACKER/start-live.sh" >> "$LOG/nifty_tracker_start.log" 2>&1
  fi
  sleep 60
done
