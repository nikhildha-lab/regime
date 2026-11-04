# S1 - Swing: pre-market plan   (08:40 IST, weekdays)

## Goal
Translate last night's setups (S2) into today's plan: which are still valid after overnight news and the opening
context, where to act, and what cancels them.

## Inputs
data_store/agent/out/swing.json (last S2 output), data_store/agent/health.json, data_store/agent/news.json (shock),
data_store/agent/out/news.json (events, stock_flags), market_pulse_live.json -> india.overnight, events,
markets.NIFTY / BANKNIFTY (exposure_regime, mtf 15m / 4h / daily, next_days.range["3"], levels), india.playbook.

## Steps
1. Shock / health / data time.
2. Overnight check: S&P, US 10Y, crude, USD/INR changes (fact); any stock_flags for setup names.
3. For each S2 setup: still valid? (regime same, 4H not turned against it, no news flag, entry zone still reachable
   inside the 3-day range). Mark valid / wait / cancelled with reason.
4. Index bias for the day (long / short / neutral) from regime + 4H, with the level that flips it.

## Output -> data_store/agent/out/swing.json   (keeps S2 setups, updates status)
{"persona":"swing","activity":"S1","as_of":"...","data_time":"...","headline":"...",
 "bias":{"NIFTY":"long|short|neutral","BANKNIFTY":"..."},"bias_flip_level":{"NIFTY":0,"BANKNIFTY":0},
 "setups":[{ trade-prep card + "status":"valid|wait|cancelled","status_reason":"..." }],
 "points":[{"text":"...","label":"..."}]}
