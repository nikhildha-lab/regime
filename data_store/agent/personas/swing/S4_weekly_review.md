# S4 - Swing: weekly review   (Saturday 09:15 IST)

## Goal
Honest scorecard of the week's swing setups and lessons for the rules (not for hindsight).

## Inputs
data_store/agent/activity_log.csv (this week's S1-S3 lines), data_store/agent/out/swing.json history if kept,
nse_stocks_eod.csv / sectors_day.csv / index closes for outcomes.

## Steps
1. List every setup published this week: entry zone touched? outcome at target / stop / time-stop / still open (fact).
2. Stats: count, hit rate, average R (only for setups whose entry zone was touched).
3. What the filters rejected and how those names did (were the rejections right?).
4. One or two rule observations - phrased as hypotheses to test, never as rule changes.

## Output -> data_store/agent/out/swing_weekly.json
{"persona":"swing","activity":"S4","week_ending":"...","headline":"...",
 "setups":[{"idea":"...","entered":true,"outcome":"target|stop|time-stop|open","r":0}],
 "stats":{"published":0,"entered":0,"hit_rate":0,"avg_r":0},"rejections_check":"...",
 "hypotheses":["..."]}
