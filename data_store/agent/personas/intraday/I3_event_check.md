# I3 - Intraday: event check   (on trigger: 4H turn, VIX jump, sharp move, OI flip; plus 12:45 and 14:45)

## Triggers this activity handles
4H early turn, India VIX jump, sharp index move, OI flip, and order-flow events from the engine: wall_pulled /
wall_consumed at a key level, repeated absorption (2x+ at the same price), possible iceberg at a key level, big prints.

## Goal
Answer one question fast: does this event change today's intraday plan or open ideas? 5 lines max.

## Inputs
The trigger text (from the caller), current intraday.json, tracker snapshot.json, order-flow engine
(of_snapshot, of_events(minutes=10)), live quotes, news.json shock,
market_pulse_live.json mtf 15m / 4h + exposure_regime.

## Steps
1. What happened (fact, with time). 2. Tested / watch meaning (e.g. 4H turned down while daily up -> daily often
follows within 5 sessions). 3. Effect on open ideas (keep / tighten stop / exit) and on the plan. 4. After 14:45:
exits and risk only, no new ideas.

## Output -> data_store/agent/out/intraday.json (append to "events")
{"activity":"I3","as_of":"...","trigger":"...","headline":"...","effect_on_ideas":[{"idea":"...","action":"keep|tighten|exit"}],
 "plan_change":"...","labels":["..."]}
