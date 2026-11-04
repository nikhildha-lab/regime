# G3 Weekly guardrail review (Saturday 20:55, after the Learner's weekly run)

## Goal
Audit the Learner: every memory and addenda change this week is evidence-backed, tightening-only, and logged.

## Steps
1. Read memory/changelog.md entries of the last 7 days. For each change:
   - lesson: evidence meets the bar (market n >= 12 graded calls with a gap vs baseline; process >= 3 repeats; mistake = a BLOCK)?
   - addendum (memory/addenda/*.md, memory/guardrails_addenda.md): does it only tighten? Does it contradict 00_COMMON_RULES?
   - Does any change alter an evidence label, raise size, or relax a rule? -> REVERT REQUEST (you cannot edit it: write it
     to memory/proposals.md as "Governor: revert <id> because ...", and flag it first in your output).
2. Memory health: active lessons <= 40, addenda <= 15 lines per persona, lessons past their review date listed.
3. Week compliance: average daily compliance, top 3 repeat problems, whether last week's for_learner items were addressed.
4. Proposals waiting for Nikhil: list them with one-line summaries.

## Output -> data_store/agent/out/governance_weekly.json
{"persona":"governance","activity":"G3","as_of":"...","verdict":"...","changes_reviewed":[{"id":"...","ok":true,"why":"..."}],
 "revert_requests":["..."],"memory":{"active_lessons":0,"overdue_reviews":["..."]},"week_compliance_pct":0,
 "repeat_problems":["..."],"proposals_waiting":["..."]}
Then log the run with agent_log.py (activity G3). Phone: 3 lines - verdict, any revert request, proposals waiting.
