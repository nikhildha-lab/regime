# Forward (front-window) paper testing

Run the tracker LIVE during market hours; it paper-trades every qualified signal
(score ≥ 4) and records each with its day-type, entry/exit, and timing. After each
session — or after a week — score those live trades through the exact vehicle
model used in the backtest, so your forward results are directly comparable to the
2-year backtest.

## Each trading morning
```bash
cd ~/Documents/NSE && python3 kite_login.py                 # refresh Kite token (PIN+TOTP)
cd ~/Documents/nifty-live-tracker && source .venv/bin/activate
RUN_MODE=live python run.py                                 # dashboard + Telegram + paper log
```
(or let the launchd job auto-start it — see `scheduler/README.md`.)

The live paper trades accumulate in `paper/paper_trades.jsonl` across days — this
is your forward-test sample. Do NOT delete it between days; let it build.

## Analysing the forward test (any time)
```bash
# vehicle comparison on the LIVE paper trades (option-buy / synthetic / smart hybrid)
python -m src.compare_vehicles --label forward --trades paper/paper_trades.jsonl

# month/quarter breakdown + consistency on the live trades
python -m src.period_report --label forward --trades paper/paper_trades.jsonl
open backtests/forward/period_report.html
```

## What you're checking
The forward test is the real out-of-sample proof. Watch for:
- Does the **smart hybrid** stay positive live, at a similar win rate (~42–45%)?
- Does the **day-type routing** (synthetic on Trend/Neutral, buy elsewhere) still help?
- Do **Normal-Variation days** still bleed (confirming the skip rule)?
If forward numbers roughly track the backtest, the edge is real. If they diverge
hard, the backtest was curve-fit — better to learn it on paper than with money.

Forward testing costs nothing and risks nothing. Give it at least 15–20 trading
sessions before drawing conclusions — a handful of days is noise.
