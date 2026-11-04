# P1 - Positional: daily close review   (15:55 IST, weekdays)

## Goal
Decide whether today's close changes the positional stance for Nifty / Bank Nifty and the watchlist. Usually it does not.

## Inputs
- data_store/agent/health.json, data_store/agent/news.json (shock), data_store/agent/out/news.json (stock_flags, events)
- data_store/dashboard/market_pulse_live.json:
  markets.NIFTY / BANKNIFTY.exposure_regime (regime, days_in_regime, size_blend, candidate_v2.{size_v2, bear_confirmed,
  bear_signals, risk_warnings, relief_warnings}), mtf.rows (daily, weekly, monthly), next_days.range["5"], levels;
  india.stocks.breadth, india.stocks.positional, india.sectors.rows, india.overnight (US 10Y), events
- data_store/india/results_calendar.json (next 21 days)
- data_store/agent/out/positional.json (yesterday's output) - to report what changed

## Steps
1. Health / shock / staleness check (rules file section 3).
2. Per index: regime + day count, engine size vs v2, bear confirmation count, active risk / relief warnings.
3. Compare with yesterday: did regime, bear confirmation, breadth band (<30 / 30-60 / >60 % above 200-day) or a
   warning change? If none changed -> stance "Hold / no change" with a one-line reason.
4. Watchlist hygiene: drop names that closed below their 200-day or have results within 3 sessions; flag
   stock_flags from the News Watcher.
5. At most 1 new idea, only if regime is not High_Volatility / Bear and the stock is a leader with improving or
   steady results trend.

## Output -> data_store/agent/out/positional.json
{"persona":"positional","activity":"P1","as_of":"YYYY-MM-DD HH:MM","data_time":"...",
 "stance":{"NIFTY":"Add|Hold|Reduce|Stay out","BANKNIFTY":"..."},"changed_today":true|false,
 "headline":"...","points":[{"text":"...","label":"tested|watch|context|fact"}],
 "watchlist":[{"symbol":"...","status":"keep|drop|new","why":"...","labels":["..."]}],
 "ideas":[ trade-prep card per rules section 5 ],"changes_view":"..."}

## Quality bar
Headline <= 20 words. If nothing changed, say so in the headline. Max 6 points.
