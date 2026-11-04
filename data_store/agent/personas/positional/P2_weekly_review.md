# P2 - Positional: weekly book review   (Saturday 09:00 IST)

## Goal
A one-page weekly view: where we are in the cycle, what the book should look like next week, and how last week's
stance and ideas performed (honest scorecard).

## Inputs
Everything in P1, plus: data_store/agent/out/positional.json history via data_store/agent/activity_log.csv (this week's
P1 headlines), data_store/paper/candidate_v2_log.csv (today's engine vs v2 sizes this week), data_store/agent/out/sectors.json
(P4), data_store/agent/out/results_scan.json (P5, if results season).

## Steps
1. Week in one line: index weekly change (from mtf weekly close), regime path (e.g. Range day 4 -> High_Vol day 2).
2. Scorecard: each idea from this week's P1 outputs -> price now vs entry zone mid, hit invalidation? (fact).
   Stance check: did "Reduce/Stay out" days precede weakness? Be honest; no cherry-picking.
3. Next week: stance per index with the 2-3 triggers that would change it; sectors to favour / avoid (from P4);
   events calendar (RBI, FOMC, CPI, results of index heavyweights).
4. Watchlist for next week: max 8 names, each with strength reason + business reason + label.

## Output -> data_store/agent/out/positional_weekly.json
{"persona":"positional","activity":"P2","week_ending":"YYYY-MM-DD","headline":"...",
 "week_summary":"...","scorecard":[{"idea":"...","entry_mid":0,"now":0,"result_pct":0,"invalidated":false}],
 "stance_next_week":{"NIFTY":"...","BANKNIFTY":"..."},"triggers":["..."],
 "sectors":{"favour":["..."],"avoid":["..."]},"events":["..."],
 "watchlist":[{"symbol":"...","strength":"...","business":"...","labels":["..."]}]}
