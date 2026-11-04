#!/bin/bash
# Start the live tracker for one market session, then auto-exit after close.
# Meant to be launched by launchd (see com.nikhil.niftytracker.plist) each
# weekday morning. Reuses your ~/Documents/NSE Kite login for the token.

PROJ="$HOME/Documents/nifty-live-tracker"
NSE_TOKEN="$HOME/Documents/NSE/data/kite_token.json"
LOG="$PROJ/scheduler/run.log"

cd "$PROJ" || exit 1
mkdir -p scheduler
echo "=== $(date) starting ===" >> "$LOG"

# Kite tokens expire daily (~6 AM IST). If today's token isn't present, we
# can't stream — notify and exit (kite_login needs your PIN+TOTP interactively).
TODAY=$(date +%Y-%m-%d)
if ! /usr/bin/grep -q "\"date\": \"$TODAY\"" "$NSE_TOKEN" 2>/dev/null; then
  osascript -e 'display notification "Kite token is stale — run: cd ~/Documents/NSE && python3 kite_login.py" with title "Nifty Tracker: refresh token"' 2>/dev/null
  echo "token stale for $TODAY — aborting" >> "$LOG"
  exit 0
fi

source .venv/bin/activate
# Stream live; auto-shutdown at 15:40 IST (after the 15:30 close + wind-down).
RUN_MODE=live python run.py --mode live --no-browser --exit-at 15:40 >> "$LOG" 2>&1
echo "=== $(date) exited ===" >> "$LOG"
