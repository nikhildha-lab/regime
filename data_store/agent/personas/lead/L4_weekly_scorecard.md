# L4 - Desk Lead: weekly scorecard   (Saturday 10:00 IST)
Inputs: out/positional_weekly.json (P2), out/swing_weekly.json (S4), this week's intraday_review files (I5) via
activity_log.csv, candidate_v2_log.csv, desk_log.csv. Steps: 1. Week in 3 lines. 2. Scorecard per persona (ideas,
hit rate, avg R, stance accuracy) - facts only. 3. Data / system issues this week (from health history if kept).
4. Next week: stance, events, top 3 things to watch. 5. Hypotheses for research (never rule changes).
Output -> data_store/agent/out/lead_weekly.json
{"activity":"L4","week_ending":"...","headline":"...","scorecard":{"positional":{},"swing":{},"intraday":{}},
 "issues":["..."],"next_week":{"stance":{},"events":["..."],"watch":["..."]},"hypotheses":["..."]}
