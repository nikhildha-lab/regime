# I5 - Intraday: post-session review   (15:40 IST, weekdays)

## Goal
Score today's intraday plan and ideas honestly, and log observations that feed the order-flow tests later.

## Inputs
Today's intraday.json (I1 / I2 / I3 entries), index 5m / 15m closes (market_pulse_live.json mtf or tracker candles5),
tracker snapshot.json (day_type, IB, VWAP), order-flow day summary (of_day_summary or data/of/<date>/summary.json:
key levels, CVD path, event counts) and the running signal check (data/of/signal_stats.csv via of_day_summary).

## Steps
1. Which plan branch played out (fact). 2. Each idea: entered? outcome (stop / target / time stop) in points and R.
3. Order-flow scorecard: which clues appeared at today's turning points and which were noise; quote the running
   signal check for each clue kind (n, hit rate, avg move) and say "too early" below 60 sessions.
4. Order-flow observations worth testing later (e.g. "CVD divergence at 11:10 preceded a 0.6% drop") - phrased
as observations, labelled context. 5. Recording health: tracker and order-flow engine ran all day? (health.json, of_status, summary.json present).

## Output -> data_store/agent/out/intraday_review.json
{"persona":"intraday","activity":"I5","date":"...","headline":"...","branch_played":{"NIFTY":"...","BANKNIFTY":"..."},
 "ideas":[{"idea":"...","entered":true,"outcome":"...","points":0,"r":0}],"observations":[{"text":"...","label":"context"}],
 "recording_ok":true}
