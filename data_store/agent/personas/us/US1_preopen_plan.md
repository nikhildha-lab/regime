# US1 Pre-open plan (18:40 IST weekdays; 19:40 IST from 1 Nov)

## Inputs
us_desk (playbook, panel, rules), score_v2_markets.US, markets.ES / NQ / US_STOCKS (next_days, levels, mtf), news.json,
agent/out/us.json from the previous night (US3), memory files (run protocol).

## Steps
1. Health / shock / halt first (common rules). If VIX data is older than 2 sessions, say so.
2. Regime and size for S&P 500 and Nasdaq; tested warnings (VIX +15%); score v2 character on 1h / daily.
3. Today's events in IST (CPI, jobs, FOMC, mega-cap earnings after the close).
4. Levels: 5-day range, supports / resistances; overnight futures move since the last close.
5. 0-2 ideas for SPY / QQQ or a named stock (swing / positional), each registered with agent_log.py call, horizon in days
   (e.g. 5d), "preplanned": true. Or an explicit no-trade call with the condition you wait for.

## Output -> data_store/agent/out/us.json (replace)
{"persona":"us","activity":"US1","as_of":"...","headline":"...","regime":{"SP500":"...","NDX":"..."},"size":{"SP500":0,"NDX":0},
 "points":[{"text":"...","label":"tested|watch|context|fact"}],"events_ist":["..."],"levels":{"SP500":{"range_5d":"..","support":[..],"resistance":[..]}},
 "ideas":[{trade-prep card + "call_id","preplanned":true}],"watch":"..."}
Phone: 2 lines - regime / size and the one thing to watch tonight.
