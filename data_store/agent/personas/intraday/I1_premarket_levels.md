# I1 - Intraday: pre-market levels and plan   (08:50 IST, weekdays)

## Goal
A level map and if-then plan for the session for Nifty and Bank Nifty, before the open.

## Inputs
market_pulse_live.json -> markets.NIFTY / BANKNIFTY (exposure_regime + candidate_v2, mtf, next_days.range["3"], levels),
india.overnight, events (weekly_expiry); tracker snapshot.json prior_levels (pdh, pdl, pwh, pwl) and oi (call_wall,
put_wall, max_pain, pcr) from the previous session; data_store/agent/out/news.json; health.json; news.json shock.

## Steps
1. Permission line per index (regime, size, warnings).
2. Level map (fact): previous day high / low / close, prior week high / low, OI call wall / put wall, max pain,
   3-day expected range low / high. Sort into a ladder.
3. If-then plan (max 3 lines per index): e.g. "If 15m holds above PDH with put writing -> long tilt toward call wall;
   if it fails back below PDH -> no trade until IB forms".
4. Expiry / event notes.

## Output -> data_store/agent/out/intraday.json
{"persona":"intraday","activity":"I1","as_of":"...","session":"pre-open","headline":"...",
 "permission":{"NIFTY":"...","BANKNIFTY":"..."},
 "levels":{"NIFTY":[{"price":0,"what":"PDH|call wall|range high|..."}],"BANKNIFTY":[...]},
 "if_then":{"NIFTY":["..."],"BANKNIFTY":["..."]},"notes":["..."],"ideas":[]}
