# N1 - News: pre-market digest   (08:05 IST, weekdays)

## Goal
In 1 minute of reading: what happened overnight that matters for Indian markets today, and what is scheduled.

## Inputs
news.json (top, shock_items, nse_announcements), market_pulse_live.json -> india.overnight (S&P 500, US 10Y, Brent,
USD/INR, US VIX, dollar index), events; india.stocks lists + out/positional.json / out/swing.json watchlists; health.json.

## Steps
1. Shock check. 2. Overnight global in 2 lines (facts with numbers). 3. India-specific news that matters (max 4).
4. Calendar: today + next 2 sessions (RBI, Fed, CPI, results of index heavyweights, expiry) and who must wait.
5. Stock flags for watchlist names (results today, block deals, pledges, rating actions).

## Output -> data_store/agent/out/news.json
{"persona":"news","activity":"N1","as_of":"...","shock_mode":false,"headline":"...",
 "overnight":["..."],"india":[{"what":"...","severity":2,"sources":["..."],"exposed":["..."]}],
 "events_next_3_sessions":[{"when":"...","event":"...","who_waits":"swing|positional|intraday|all"}],
 "stock_flags":[{"symbol":"...","news":"...","action":"avoid entry until ..."}],"label":"context"}
