# S3 - Swing: open-idea follow-up   (12:30 IST, weekdays)

## Goal
Midday check of open swing ideas only. No new ideas. Flag anything that needs Nikhil's attention now.

## Inputs
data_store/agent/out/swing.json (setups with status valid / open), data_store/india/live_quotes.json (stock / index prices),
market_pulse_live.json mtf 15m / 4h for the indices, data_store/agent/news.json (shock, new severity-2/3 items),
data_store/agent/health.json.

## Steps
1. For each open idea: live price vs entry zone, invalidation, target (fact). Distance in % and in ATR if available.
2. Alerts: price within 0.3% of invalidation; target reached; 4H turned against the idea; news on the name.
3. If nothing needs attention: headline "All open swing ideas on track" and stop.

## Output -> data_store/agent/out/swing_followup.json
{"persona":"swing","activity":"S3","as_of":"...","headline":"...",
 "ideas":[{"idea":"...","price":0,"to_invalidation_pct":0,"to_target_pct":0,"flag":"none|near-stop|target|4h-against|news"}]}
