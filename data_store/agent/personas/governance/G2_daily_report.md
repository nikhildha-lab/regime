# G2 Daily governance report (16:55 weekdays, inside the G1 run)

## Steps
1. Compliance today: runs expected vs logged (from INDEX.md schedule), outputs PASS / WARN / BLOCK counts,
   compliance score = share of outputs with no BLOCK and no escalated WARN.
2. Calls: registered today, ideas without call_id (should be 0), total size per index vs engine size at any time.
3. Data quality: health.json problems during the session, stale-data mentions, feeds missing.
4. Cost: Claude API calls and $ today (data_store/agent/api_usage.jsonl) vs agent_config.json caps.
5. Repeats: any WARN / BLOCK code seen 3+ times in the last 5 sessions (logs/governance_log.csv) -> "for_learner".

## Output -> data_store/agent/logs/governance_daily/<YYYY-MM-DD>.md (new file) and add to out/governance.json:
"daily":{"compliance_pct":0,"runs_expected":0,"runs_logged":0,"pass":0,"warn":0,"block":0,"calls":0,
         "calls_missing_id":0,"api_calls":0,"api_usd":0,"repeat_problems":["..."]}
