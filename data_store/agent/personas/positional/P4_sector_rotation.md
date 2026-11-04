# P4 - Positional: sector rotation map   (Saturday 09:30 IST)

## Goal
Show where money is moving across NSE sectors and what that implies for the book. Context only - rotation was not
tested as a timing signal; use it to choose WHERE, not WHETHER.

## Inputs
india.sectors.rows (quadrant, rs20_pct, rs60_pct, rs_momentum, ret5 / ret20), data_store/india/sectors_day.csv
(history for 4-week quadrant path), india.stocks.positional leaders (to map leaders to sectors when obvious),
Tapetide sector / index valuations if available, events.

## Steps
1. Quadrant table now and 4 weeks ago (recompute from sectors_day.csv: 60-day RS and 20-day RS change).
2. Rotation story in 3 lines: defensives (FMCG, Pharma, IT, Healthcare) vs cyclicals (Bank, PSU Bank, Metal, Realty,
   Auto, Energy, Infra). Risk-on / risk-off / mixed.
3. Movers: sectors that changed quadrant this week (e.g. Improving -> Leading).
4. Implication: favour 2-3 sectors, avoid 2-3, with one reason each; note any sector at an extreme of its 1-year range.

## Output -> data_store/agent/out/sectors.json
{"persona":"positional","activity":"P4","as_of":"...","regime_tone":"risk-on|risk-off|mixed","headline":"...",
 "quadrants_now":{"Leading":["..."],"Weakening":["..."],"Improving":["..."],"Lagging":["..."]},
 "changes_4w":[{"sector":"...","from":"...","to":"..."}],"favour":[{"sector":"...","why":"..."}],
 "avoid":[{"sector":"...","why":"..."}],"label":"context"}
