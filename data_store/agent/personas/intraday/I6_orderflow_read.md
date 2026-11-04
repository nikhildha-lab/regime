# I6 - Intraday: order-flow read   (on request - "what's order flow saying on Nifty?" - and inside I2 / I3)

## Goal
A 30-second, plain-English read of order flow for one index, tied to the levels that matter today, honest about
what is a clue and what is noise.

## Inputs
MCP tools if available, else files (see PERSONA "Order-flow engine"): of_status, of_snapshot(symbol),
of_events(symbol, minutes=30), of_footprint(symbol, minutes=15), of_liquidity(symbol, minutes=60),
of_day_summary() signal check; today's I1 levels (data_store/agent/out/intraday.json); market_pulse_live.json
exposure_regime for permission; health.json; news.json shock.

## Steps
1. Freshness: engine running and data < 10 s old? Else: "Order flow not available: <reason / fix>" and stop.
2. Now: imbalance (5 / 20 / deep) in words; delta last 5 and 15 min; CVD direction vs price (divergence?).
3. Levels: liquidity zones (largest persistent bids / asks) and which of today's I1 levels they line up with.
4. Last 30 min events, grouped by level: walls new / pulled / consumed, absorption (count), possible icebergs, big prints.
5. Read: "<level> looks defended by buyers / sellers" only with 2 independent clues; otherwise "watch <level>".
6. What would change the read (one line), and the regime permission reminder.
7. Signal check line: for the clue kinds you used, n and hit rate so far, "too early" if < 60 sessions.

## Output (reply text, or data_store/agent/out/intraday.json "orderflow" key when run on schedule)
{"activity":"I6","as_of":"...","symbol":"...","data_age_s":0,"headline":"<= 20 words",
 "now":{"imbalance":"...","delta_5m":0,"delta_15m":0,"cvd_vs_price":"confirming|diverging|flat"},
 "zones":{"support":[{"price":0,"why":"..."}],"resistance":[{"price":0,"why":"..."}]},
 "events_30m":[{"time":"...","kind":"...","price":0,"text":"..."}],
 "read":"...","changes_view":"...","signal_check":[{"clue":"...","n":0,"hit_rate":0,"status":"too early|..."}],
 "label":"context"}

## Never
Present a single pulled wall or iceberg as proof · ignore the daily regime · use order flow older than 10 s.
