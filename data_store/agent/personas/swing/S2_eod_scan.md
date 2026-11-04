# S2 - Swing: end-of-day setup scan   (15:50 IST, weekdays)

## Goal
Find at most 3 swing setups for the next 3-10 sessions, ranked by evidence, and review today's open ideas.

## Inputs
market_pulse_live.json -> markets.NIFTY / BANKNIFTY (exposure_regime incl. candidate_v2 warnings, mtf 15m / 4h / daily /
weekly, next_days.range["3"] / ["5"], next_days.big_move, levels, decision.pullback_zone / retest / stretched),
india.stocks.swing (pullbacks, breakouts, breakdowns), india.stocks.day (strong / weak, labels), india.sectors.rows,
india.playbook (deploy / avoid), events; data_store/india/results_calendar.json; data_store/agent/out/news.json stock_flags;
previous data_store/agent/out/swing.json.

## Steps
1. Health / shock / data time.
2. Regime permission per index (see persona lens 1) and 4H early-turn status (tested).
3. Candidate pool: index (pullback / range fade / weak-index short), stock pullbacks, retests, weak-stock shorts.
4. Filter: results within 10 sessions -> out; sector already used -> out; reward / risk < 1.5 inside the 5-day
   expected range -> out; against a fresh 4H turn -> out.
5. Rank by evidence: tested inputs supporting > watch > context only. Keep max 3.
6. Review open ideas: hit target / invalidated / time stop / keep.

## Output -> data_store/agent/out/swing.json
{"persona":"swing","activity":"S2","as_of":"...","data_time":"...","headline":"...",
 "bias":{"NIFTY":"...","BANKNIFTY":"..."},
 "setups":[{"instrument":"...","direction":"long|short","setup":"pullback|retest|weak-stock|range-fade|index",
            "why":"...","regime_fit":"...","entry_zone":"...","invalid_if":"...","target":"...","time_stop":"10 sessions",
            "rr":0,"size_hint":"...","labels":["..."],"status":"new","you_decide":true}],
 "open_ideas_review":[{"idea":"...","status":"open|target|invalidated|time-stop","note":"..."}],
 "rejected":[{"name":"...","reason":"..."}],"points":[{"text":"...","label":"..."}]}
Listing the main rejected candidates (max 5) is required - it shows the filter worked.
