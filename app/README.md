# Market Pulse - live local app

## Start
Double-click **Start Market Pulse.command** in this folder (or run `./run_market_pulse.sh` in Terminal).
The first start sets itself up (about a minute), then opens http://127.0.0.1:8050 in your browser.
Keep the Terminal window open; close it (or press Ctrl+C) to stop.

If macOS says the file "can't be opened because it is from an unidentified developer": right-click it, choose Open, then Open.

## What it does
Every 5 minutes (`--every 5` to change):
1. `app/updater.py` fetches new prices: Yahoo (Nifty, Bank Nifty, India VIX, crypto, gold, silver, copper, crude,
   S&P, Nasdaq, US macro), Upstox (Nifty / Bank Nifty futures, 5 minute) and, every 30 minutes, NSE end-of-day files
   (futures bhavcopy, FII/DII participant OI, stock closes for breadth).
2. `scripts/export_dashboard_data.py --as-of now` recomputes every regime, timeframe and strategy recommendation.
3. The page redraws itself when new numbers are ready. "Refresh now" runs a cycle straight away.

Only finished bars are used, and the engine's point-in-time rule still applies (a bar counts only once it has closed).

## First start notes
- The first NSE run backfills stock closes since March 2026 (~150 small files, a few minutes). Breadth shows
  "no data" until that finishes.
- If a site can't be reached, the page says so and keeps showing the last data it has.

## Files
| File | Role |
|---|---|
| `app/server.py` | local web server (127.0.0.1 only) + background refresh loop |
| `app/updater.py` | data fetchers; writes into `data_store/market_data/*.csv` |
| `dashboard/market_pulse_template.html` | the page (same file is used for static snapshots) |
| `data_store/dashboard/market_pulse_live.json` | latest computed dashboard data |
| `data_store/market_data/update_status.json` | last fetch result per source |

Nothing here places orders. Not investment advice.
