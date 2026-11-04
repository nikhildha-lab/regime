# Scheduling the live analyst

Two independent pieces:

## 1. Telegram qualified-setup alerts (the analyst that messages you)
The running app already messages you during live hours — no schedule needed once
it's running. When a **qualified** signal fires (score ≥ `alerts.min_score`,
default 3) it sends a Telegram message with the full plan:

```
🔴 SHORT  BANKNIFTY @ 55093  (score 6)
trend down (LH/LL); ORB breakout down; below VWAP; IB range-extension down
• FUTURES (short) or BUY_PUT (ATM/ITM)
• Entry 55093 | SL 55148 | T1 54978 → 54900 | R:R 2.0
• Directional day — clean move expected.
```

At market close it sends an **EOD summary** of the day's paper-trade performance.

Set up Telegram once (see main README → Alerts): put `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` in `.env`. Tune the bar with `alerts.min_score` in `config.yaml`.

## 2. Auto-start every weekday (so you don't have to)
`run_market_hours.sh` + the launchd job start the tracker at **09:12 IST** each
weekday and auto-exit at **15:40**. Live streaming reuses your `~/Documents/NSE`
Kite login (no second token to manage).

```bash
chmod +x ~/Documents/nifty-live-tracker/scheduler/run_market_hours.sh
cp ~/Documents/nifty-live-tracker/scheduler/com.nikhil.niftytracker.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.nikhil.niftytracker.plist
```

The one daily manual step is the **token refresh** (`cd ~/Documents/NSE &&
python3 kite_login.py`) because Kite's login needs your PIN + TOTP. If the token
isn't fresh at 09:12, the script sends you a macOS notification to refresh it
instead of starting blind. (You can fully automate this later with a TOTP-based
Kite login, but that's out of scope here.)

A **Cowork scheduled task** also pings you pre-market each weekday as a reminder
to refresh the token and confirm the tracker is up.
