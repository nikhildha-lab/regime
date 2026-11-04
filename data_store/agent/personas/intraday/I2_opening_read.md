# I2 - Intraday: opening read   (09:25 and 10:20 IST)

## Goal
Read the open (09:25) and the initial balance (10:20); confirm or cancel the I1 if-then plan; at most 1 idea per index.

## Inputs
I1 output (data_store/agent/out/intraday.json), tracker snapshot.json (price_action.orb_*, vwap, market_profile.ib_high /
ib_low / day_type, oi.buildup / options_bias, participation), order-flow engine: of_snapshot + of_events(minutes=15)
+ of_footprint(minutes=10) for each index (or data/of/live.json),
market_pulse_live.json mtf 5m / 15m / 4h, live quotes, news.json shock, health.json.

## Steps
0. Call `get_mtf_regime` for NIFTY and BANKNIFTY (MCP synaptic-regime; else market_pulse_live.json -> score_v2). Lead with the 5m / 15m CHARACTER (tested) and say what it means for size and stops; Nifty 5m / 15m direction as a watch tilt only.
1. Data freshness (tracker generated_at <= 5 min) - else order-flow part "not available".
2. Gap and opening type; price vs VWAP; ORB status; (10:20) IB width vs normal and day-type read.
3. Options flow last 60 min (watch) and OI build-up (context). Order flow (context): book imbalance, walls near the
   I1 levels (held / pulled / consumed), absorption at the opening range edges, CVD vs price divergence, stacked
   footprint imbalances. Use the 2-clue rule from the persona.
4. Which I1 if-then branch is live? Idea only if: regime permits, a level is defined, and at least one tested or watch
   input agrees. Otherwise "no trade yet - wait for <condition>".

## Output -> data_store/agent/out/intraday.json (update)
{"persona":"intraday","activity":"I2","as_of":"...","session":"open|ib","headline":"...",
 "read":{"NIFTY":{"gap_pct":0,"vs_vwap":"above|below","orb":"...","day_type":"...","flow":"...","cvd":"...","depth":"..."},"BANKNIFTY":{}},
 "plan_branch":{"NIFTY":"...","BANKNIFTY":"..."},
 "ideas":[{ trade-prep card with "stop":0,"time_stop":"..." }],"points":[{"text":"...","label":"..."}]}
