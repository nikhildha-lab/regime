# LR2 Weekly improvement (Saturday 19:10)

## Steps
1. Run LR1 steps 1-7 first.
2. Weekly scorecard per persona: calls, hit %, avg R after costs, baseline R (market entry at call time), calibration,
   not-filled share, range-call hold rate vs its stated rate, no-trade calls that were "quiet".
   Verdict per persona: too early (< 30 graded) / adding value / not adding value.
3. Prompt enrichment (tightening only): for each active lesson that applies to one persona, make sure its addenda file has
   a one-line instruction that puts it into practice (and quotes the lesson id). Remove addenda whose lesson was retired.
4. Proposals (loosening or structural): e.g. "re-allow ORB entries on Volatile days - last 20 such setups +0.4 R",
   "the Swing persona is not adding value after 40 calls - simplify S2", "move a scheduled run". Write each to
   memory/proposals.md with what / why / evidence / exact change / how to undo. Max 3 per week.
5. Approved proposals (status: approved by Nikhil): apply them (addenda or memory only), log with agent_log.py change,
   mark "applied <date>". Code, schedule and settings changes: leave a note for Nikhil - you cannot make them.
6. Write logs/learning_weekly/<YYYY-MM-DD>.md: the week in 5 lines, scorecard table, lessons added / retired, proposals.

## Output -> data_store/agent/out/learning_weekly.json
{"persona":"learning","activity":"LR2","as_of":"...","by_persona":{"intraday":{"n":0,"hit_pct":null,"avg_r_net":null,
 "verdict":"too early"}},"lessons_added":["..."],"lessons_retired":["..."],"addenda_changes":["..."],
 "proposals_new":["P-0000 ..."],"proposals_applied":["..."],"headline":"..."}
Phone: 3 lines - which persona adds value (or "too early"), top new lesson, proposals waiting for Nikhil.
