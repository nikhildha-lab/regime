# N2 - News: hourly shock check   (10:05-15:05 IST hourly)

## Goal
Silence unless something big happened. Be cheap on quiet hours.

## Steps
1. Read news.json: shock.active and new severity-3 items since the last run (field "new").
2. Nothing new and big -> write {"persona":"news","activity":"N2","as_of":"...","shock_mode":false,
   "headline":"No big news since the last check."} and STOP.
3. Otherwise run the N4 analysis steps (confirm, exposure, desk rule) and write the full output.
4. If the market moved > 1% intraday with no big headline: "move_explained": best supported reason from 2+ sources,
   or "no clear news reason".

## Output -> data_store/agent/out/news.json (same schema as N4)
