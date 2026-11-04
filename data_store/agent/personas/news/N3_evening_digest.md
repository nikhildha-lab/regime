# N3 - News: evening digest   (18:05 IST, weekdays)

## Goal
Close the day: what explained today's move, results and corporate actions that matter, tomorrow's calendar.

## Inputs
news.json (top, nse_announcements), data_store/india/results_calendar.json, results_summary.json (today's reporters),
market_pulse_live.json (index closes, events), watchlists.

## Steps
1. Today's move explained (fact + best supported reason or "no clear news reason").
2. Results today among F&O stocks: in one line each (defer detail to Positional P5).
3. Corporate actions / announcements on watchlist names.
4. Tomorrow and next 2 sessions: events and who waits.

## Output -> data_store/agent/out/news.json
{"persona":"news","activity":"N3","as_of":"...","shock_mode":false,"headline":"...","move_explained":"...",
 "results_today":[{"symbol":"...","line":"..."}],"stock_flags":[...],"events_next_3_sessions":[...],"label":"context"}
