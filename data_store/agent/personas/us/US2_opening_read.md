# US2 Opening read (20:15 and 21:15 IST weekdays; one hour later from 1 Nov)

## Steps
1. Gap vs yesterday's close and vs the pre-open plan levels; first 30 / 60 minutes direction and range (5m / 15m v2 = context).
2. 1-hour v2 character (tested: Volatile -> bigger moves) and VIX move since the open.
3. Check US1 ideas: still valid? Flag near-stop / triggered. New ideas only if they were preplanned or before 23:30 IST.
## Output -> agent/out/us.json: update "open_read":{"at":"...","read":"...","ideas_status":[{"call_id":"...","status":"..."}]}
Phone: only if an idea triggered, a stop is near, or VIX jumped > 10% intraday.
