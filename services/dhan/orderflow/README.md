# Order Flow Backtester (NIFTY / BANKNIFTY)

Volume profile + order flow setups, tested 4 ways: NIFTYBEES/BANKBEES MIS,
debit spread, credit spread, naked option sell.

## Every time you open Terminal
    cd ~/Documents/dhan && source venv/bin/activate && cd orderflow

## 1. Download history (once, then weekly to top up)
    python download_history.py
Takes a while (many small API calls for options). Safe to stop and re-run.
If the options part says "no data" for every month, open config.py and set
EXPIRY_CODE = 0, then run again.

## 2. Run the backtest
    python backtest.py
    open report.html

## 3. Record live ticks (every trading day, start before 9:15)
    python recorder.py
Stops by itself at 15:31. Saves to data/ticks/. This becomes real footprint data.

## 4. Paper trade live (no real orders) - every trading day
    python live.py
Runs the 4 setups on live futures ticks with REAL buy/sell delta, takes paper
trades, logs to data/paper/. Replay a recorded day: python live.py --replay data/ticks/2026-10-15.csv

## 5. VIX check (after backtest.py)
    python vix.py

## 6. Rule scanner (2,000+ VAH/VAL variations, with luck filter)
    python scan.py

## Files
- config.py: all settings (times, thresholds, lot sizes, costs)
- engine.py: volume profile (POC/VAH/VAL), initial balance, delta, CVD
- setups.py: the 4 setups
- backtest.py: trade simulation + report
- download_history.py: Dhan history download
- recorder.py: live tick recorder
- live.py: paper-trading engine on live ticks
- scan.py: scenario scanner
- vix.py: VIX vs results
