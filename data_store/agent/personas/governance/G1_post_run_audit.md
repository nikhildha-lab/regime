# G1 Post-run audit (08:55, 10:55, 12:55, 16:55 weekdays; Saturday 10:55 for the weekly outputs)

## Goal
Before Nikhil reads the desk, confirm every output written since your last audit is safe to read.

## Steps
1. Run `python3 scripts/agent/guard_check.py`. Read logs/guard_report.json.
2. `python3 scripts/agent/agent_log.py show`: every output written today has a run line; every idea's call_id exists.
   An output with no run line = process WARN ("run not logged").
3. For each BLOCK: name the file, rule, evidence; it stays BLOCKED (the dashboard shows it, the Lead must skip it).
4. For each WARN: decide acceptable / escalate (one reason each).
5. Spot-check 3 numbers per output with ideas against the source files (market_pulse_live.json, india/*, of/live.json).
6. Halt rule (PERSONA "Your powers"). If you set or keep a halt, say so first.
7. At 16:55 also run G2.

## Output -> data_store/agent/out/governance.json (replace)
{"persona":"governance","activity":"G1","as_of":"<IST time>","verdict":"Desk OK|Desk OK with N warnings|Desk BLOCKED: ...",
 "blocked":[{"file":"...","rule":"...","evidence":"...","owner":"<persona>"}],
 "warnings":[{"file":"...","code":"...","decision":"acceptable|escalate","why":"..."}],
 "spot_checks":[{"file":"...","number":"...","source":"...","ok":true}],
 "halt":{"active":false,"reason":null},"for_learner":["process problems seen 3+ times"],"label":"fact"}
Then: `python3 scripts/agent/agent_log.py run --persona governance --activity G1 --out data_store/agent/out/governance.json --headline "<verdict>"`.
Phone line: only when something is BLOCKED or halted ("GOVERNANCE: ..."); otherwise "Desk OK".
