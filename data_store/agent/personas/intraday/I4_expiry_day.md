# I4 - Intraday: expiry-day plan   (09:00 IST on weekly / monthly expiry days)

## Goal
Expiry days behave differently (gamma, pinning, fast late moves). Give the special rules and level map for today.

## Inputs
events.weekly_expiry, tracker snapshot.json oi (max_pain, call_wall, put_wall, chain near expiry), oc_live.json,
next_days.range["3"], exposure_regime, news.json shock.

## Steps
1. Confirm which index expires today (Nifty weekly / Bank Nifty monthly - from events / chain expiry).
2. Pin zone: max pain and the two largest OI strikes (context - pinning is not tested).
3. Rules: smaller size; no fresh short options after 14:30; avoid buying far OTM options after 13:30 (decay);
   defined-risk only if regime is High_Volatility.
4. Two scenarios (pin / break) with levels.

## Output -> data_store/agent/out/intraday_expiry.json
{"persona":"intraday","activity":"I4","as_of":"...","expiring":["NIFTY"],"headline":"...","pin_zone":{"max_pain":0,"call_wall":0,"put_wall":0},
 "rules":["..."],"scenarios":[{"name":"pin","levels":"..."},{"name":"break","levels":"..."}],"label":"context"}
