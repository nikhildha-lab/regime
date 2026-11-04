# LR1 Evening learning (19:10 weekdays)

## Steps
1. `python3 scripts/agent/grade_calls.py`. Note new grades and calls still waiting (and why).
2. Mistakes: every BLOCK in logs/guard_report.json today -> one "mistake" lesson (n=1), worded as the rule to follow,
   unless the same mistake is already in memory (then add nothing; if it repeated, add a guardrail addendum - see bar).
3. Process: read out/governance.json "for_learner" and today's run logs ("unsure", "errors", skipped runs). Count repeats in
   logs/agent_log.jsonl + logs/governance_log.csv over 10 sessions; >= 3 -> process lesson.
4. Market patterns: from calls_graded + agent_calls, test these splits only (no fishing): persona x activity; regime;
   v2 character of the call's timeframe (Volatile / Range / Trending); entry in the first 15 / 30 minutes vs later;
   with vs without a tested label; confidence bucket (calibration). For each split with n >= 12: compare avg R after costs
   vs the rest, check both halves. Meets the bar -> lesson; else -> candidate with its numbers.
5. Calibration: if stated confidence is > 15 points above the actual hit rate in a bucket with n >= 20 -> process lesson
   ("cap stated confidence at X% for <persona>").
6. Feedback: if data_store/agent/feedback.csv exists, count taken / skipped / useful per persona; low usefulness
   (< 40% useful, n >= 15) -> candidate for the weekly review.
6b. Live check: read data_store/validation/report.json. Any claim "not holding" with enough cases -> write a proposal
   (memory/proposals.md) to downgrade it (e.g. tested -> watch), with the backtest number, the live number and n. Never change a
   label yourself. Claims "holding" on untested items stay context until a research re-test passes.
7. Prune: lessons past their review date -> re-check with current data; keep (new review date) or retire.

## Output -> data_store/agent/out/learning.json (replace)
{"persona":"learning","activity":"LR1","as_of":"...","graded_today":0,"waiting":0,
 "scorecard":{"n":0,"hit_pct":null,"avg_r_net":null,"vs_baseline":null,"verdict":"too early"},
 "new_lessons":[{"id":"L-0000","kind":"mistake|process|market","text":"..."}],
 "candidates":[{"key":"...","n":0,"gap_r":0,"needs":"n >= 12"}],
 "retired":["L-0000 (why)"],"note":"one line in plain words"}
Then log the run (agent_log.py run --persona learning --activity LR1 ...). Phone: only if a new market lesson was added.
