# Desk personas and activities (v2)
Each activity has its own prompt file. Run = read 00_COMMON_RULES.md + the persona's PERSONA.md + the activity file.

| Persona | Activity | File | When (IST, weekdays) | Output (data_store/agent/out/) |
|---|---|---|---|---|
| Desk Lead | L1 Morning brief | lead/L1_morning_brief.md | 08:45 | ../desk_notes.json |
| Desk Lead | L2 Midday update | lead/L2_midday_update.md | 12:45 | ../desk_notes.json |
| Desk Lead | L3 Close summary | lead/L3_close_summary.md | 16:45 | ../desk_notes.json |
| Desk Lead | L4 Weekly scorecard | lead/L4_weekly_scorecard.md | Sat ~09:00 (after P4, P2, S4) | lead_weekly.json |
| News Watcher | N1 Pre-market digest | news/N1_premarket_digest.md | 08:05 | news.json |
| News Watcher | N2 Hourly shock check | news/N2_shock_check.md | 10:05-15:05 hourly | news.json |
| News Watcher | N3 Evening digest | news/N3_evening_digest.md | 18:05 | news.json |
| News Watcher | N4 Instant shock analysis | news/N4_instant_shock.md | on shock (Claude API) | news.json |
| Positional | P1 Daily close review | positional/P1_daily_close_review.md | 15:52 (after I5, S2) | positional.json |
| Positional | P2 Weekly book review | positional/P2_weekly_review.md | Sat 09:00 | positional_weekly.json |
| Positional | P3 Stock deep dive | positional/P3_stock_deep_dive.md | on request | deepdive_<SYMBOL>.json |
| Positional | P4 Sector rotation map | positional/P4_sector_rotation.md | Sat 09:30 | sectors.json |
| Positional | P5 Results season scan | positional/P5_results_scan.md | 18:35 (skips if no results) | results_scan.json |
| Swing | S1 Pre-market plan | swing/S1_premarket_plan.md | 08:38 | swing.json |
| Swing | S2 End-of-day setup scan | swing/S2_eod_scan.md | 15:52 | swing.json |
| Swing | S3 Open-idea follow-up | swing/S3_idea_followup.md | 12:30 | swing_followup.json |
| Swing | S4 Weekly swing review | swing/S4_weekly_review.md | Sat 09:15 | swing_weekly.json |
| Intraday | I1 Pre-market levels | intraday/I1_premarket_levels.md | 08:38 | intraday.json |
| Intraday | I2 Opening read | intraday/I2_opening_read.md | 09:25 and 10:25 | intraday.json |
| Intraday | I3 Event check | intraday/I3_event_check.md | on trigger / 12:45 / 14:45 | intraday.json |
| Intraday | I4 Expiry-day plan | intraday/I4_expiry_day.md | 09:00 on expiry days | intraday_expiry.json |
| Intraday | I6 Order-flow read | intraday/I6_orderflow_read.md | on request + inside I2 / I3 | reply / intraday.json "orderflow" |
| Intraday | I5 Post-session review | intraday/I5_post_session.md | 15:52 | intraday_review.json |
| US Desk | US1 Pre-open plan | us/US1_preopen_plan.md | 18:40 (19:40 from 1 Nov) | us.json |
| US Desk | US2 Opening read | us/US2_opening_read.md | 20:15, 21:15 (+1 h from 1 Nov) | us.json "open_read" |
| US Desk | US3 Overnight wrap | us/US3_overnight_wrap.md | 07:30 Tue-Sat | us.json "overnight" |
| Crypto Desk | C1 Daily review | crypto/C1_daily_review.md | 07:05 daily | crypto.json |
| Crypto Desk | C2/C3 Session reads | crypto/C2_session_read.md | 13:40, 19:40 (20:40 from 1 Nov) | crypto.json "session_reads" |
| Crypto Desk | C4 Weekly review | crypto/C4_weekly_review.md | Sunday 09:00 | crypto_weekly.json |
| Governor | G1 Post-run audit | governance/G1_post_run_audit.md | 08:55, 10:55, 12:55, 16:55 (Sat 10:55) | governance.json |
| Governor | G2 Daily governance report | governance/G2_daily_report.md | 16:55 (inside G1) | logs/governance_daily/<date>.md |
| Governor | G3 Weekly guardrail review | governance/G3_weekly_guardrail_review.md | Sat 20:55 | governance_weekly.json |
| Learner | LR1 Evening learning | learning/LR1_evening_learning.md | 19:10 | learning.json + memory/* |
| Learner | LR2 Weekly improvement | learning/LR2_weekly_improvement.md | Sat 19:10 | learning_weekly.json + memory/* |
| Learner | LR3 Monthly memory audit | learning/LR3_monthly_memory_audit.md | 1st Sat 19:10 (after LR2) | logs/learning_weekly/ |

Every run follows 00_COMMON_RULES section 9 (halt check, memory, call registration, guard check, run log).
Logs (data_store/agent/logs/): agent_log.jsonl (runs), agent_calls.csv (ideas), calls_graded.csv, learning_scorecard.json,
guard_report.json, governance_log.csv. Memory (data_store/agent/memory/): desk_memory.md, guardrails_addenda.md,
addenda/<persona>.md, proposals.md (Nikhil approves), changelog.md, halt.json.

Data sources (read-only): data_store/dashboard/market_pulse_live.json, data_store/india/*, data_store/agent/{health,news}.json,
~/Documents/nifty-live-tracker/snapshot.json (tracker), Dhan order-flow engine: MCP tools of_* (synaptic-orderflow) or ~/Documents/dhan/orderflow/data/of/ (live.json, per-day files, signal_stats.csv),
Tapetide connector (fundamentals, ratios, shareholding - when enabled; cross-check with NSE results_summary.json).
Twelve Data: NOT usable on the current free plan for Indian stocks (checked 2026-10-17).
