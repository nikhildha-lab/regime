# Proposals waiting for Nikhil's approval
Anything that would LOOSEN a rule, raise size, change a schedule, change code, or change a signal's evidence label.
The Learner writes here; nothing here is applied until Nikhil marks it "approved" (then the Learner applies it and logs it).
Format: ## P-0001 · YYYY-MM-DD · status: pending | approved | rejected
What / why / evidence (numbers, n) / exact change / how to undo.

## P-0001 · 2026-10-21 · status: pending
What: restore the results / announcements input (code or data fix - Learner cannot do this).
Why: NSE announcements fetch returned HTTP 403 and data_store results_calendar.json was missing in 8 runs over 2 sessions (20-21 Oct); news runs could not explain results-driven moves and positional P5 skipped twice. See L-0002.
Evidence: logs/agent_log.jsonl news N1/N2/N3, positional P5 on 20 and 21 Oct (n=8).
Exact change: Nikhil to fix the NSE fetch (headers/cookies or alternative source) or produce results_calendar.json; no prompt or rule change.
How to undo: nothing to undo; once fixed, L-0002 can be retired at its review.

## P-0002 · 2026-10-23 · status: pending
What: live check says 3 tested India claims are "not holding" - consider downgrading (or, better, wait and re-test).
Why: data_store/validation/report.json (generated 2026-10-23 18:33, live since 2026-10-20 = 4 sessions only).
Evidence: Bank Nifty 5m "Range -> smaller moves": backtest x0.81, live x1.43, n=28. Nifty 15m "Volatile -> bigger moves": backtest x1.43, live x0.97, n=24. Nifty 5m v2 direction: backtest IC +0.042, live IC -0.196, n=139 bars.
Learner read: too early to act - 4 sessions in one High_Volatility / falling stretch; 5m bars overlap heavily, so n overstates the evidence. Note the registry already lists Nifty 5m direction as "watch", not tested.
Exact change (only if Nikhil wants it now): Bank Nifty 5m Range and Nifty 15m Volatile tested -> watch; Nifty 5m direction watch -> context. Recommended instead: keep labels, research re-test after ~20 sessions of live data.
How to undo: restore the labels in 00_COMMON_RULES.md section 2 registry.

## P-0003 · 2026-10-23 · status: pending
What: fix missing / late scheduled runs (schedule or folder connection - Learner cannot change schedules).
Why: Governor for_learner 23 Oct - scheduled runs missing 4 sessions in a row (20-23 Oct); on 23 Oct US3, S1, I1, I2, US1 ran at 13:19-13:24 IST, 3-5 h late (folder not connected). See L-0005.
Evidence: out/governance.json for_learner; logs/agent_log.jsonl 23 Oct (n=5 late runs, 4 sessions).
Exact change: Nikhil to keep the platform folder connected for scheduled tasks, or move them to a machine that stays on.
How to undo: nothing to undo; retire L-0005 at review once runs are on time.
Update 2026-10-26 (learner): still happening - Governor for_learner 26 Oct: runs missing 5 sessions in a row (20-26 Oct), incl. Governor 08:55/10:55/12:55; L3 ran 16:50, N2 10:05/11:05 missing.

## P-0004 · 2026-10-23 · status: pending
What: renew the Upstox token daily (ops fix).
Why: health.json 'fail' (Upstox token expired) 3 sessions in a row, 21-23 Oct; live quotes stale in news, lead, intraday, swing runs.
Evidence: out/governance.json for_learner 23 Oct; logs/agent_log.jsonl unsure fields 21-23 Oct.
Exact change: Nikhil renews the token before 08:00 IST each trading day (or automates renewal). No prompt or rule change.
How to undo: nothing to undo.

## P-0005 · 2026-10-26 · status: pending
What: live check update - 6 tested India/crypto claims now "not holding" (3 new since P-0002). Recommend: wait and re-test, no label change yet.
Why: data_store/validation/report.json generated 2026-10-26 19:05, live since 2026-10-20 (5 sessions, mostly one High_Volatility / falling stretch).
Evidence (backtest -> live, n): Bank Nifty 5m Range smaller moves x0.81 -> x1.36 (n=38); Bank Nifty 15m Range x0.87 -> x1.06 (n=16, new); Nifty 15m Volatile bigger moves x1.43 -> x0.98 (n=47); Nifty 5m v2 direction IC +0.042 -> -0.07 (n=186 bars); Nifty 15m v2 direction IC +0.059 -> -0.228 (n=94 bars, new; registry has it as watch); BTC 4h Range smaller moves x0.81 -> x1.14 (n=20, new). Holding: Nifty 75m/1D-type Volatile bigger moves x1.4 -> x1.25 (n=76).
Learner read: too early - 5 sessions, overlapping intraday bars overstate n, one regime. Same advice as P-0002.
Exact change (only if Nikhil wants it now): Bank Nifty 5m/15m Range and Nifty 15m Volatile tested -> watch; BTC 4h Range tested -> watch; Nifty 5m/15m direction watch -> context. Recommended instead: research re-test after ~20 live sessions.
How to undo: restore the labels in 00_COMMON_RULES.md section 2 registry.

## P-0006 · 2026-10-26 · status: pending
What: fix the guard_check.py 'level' rule so the US 8% check only fires on US desk files / named US instruments (code change - Learner cannot do this).
Why: 26 Oct 08:46 guard BLOCKed desk_notes.json ideas 1-2 (Nifty 22,850-23,150 and Bank Nifty 54,950-55,422) as '>8% from ES close 7664.75'. Lead L1 notes the word 'rallies' matched the ES instrument; Governor G1/G2 judged it a misfire. A false BLOCK on the India desk makes the guard noisy and teaches personas to ignore it.
Evidence: logs/governance_log.csv 2026-10-26T08:46:59 (2 BLOCKs); logs/agent_log.jsonl lead L1 26 Oct unsure; out/governance.json for_learner 26 Oct ('level' BLOCK 4 times in 5 sessions, 2 of them India false positives).
Exact change: match instrument names as whole words (e.g. \bES\b), and apply the US level check only to us.json or to ideas whose instrument is ES/NQ/SPX/NDX/SPY/QQQ. G-0001 (US desk only) stays as written.
How to undo: revert guard_check.py to the previous version.


## P-0007 · 2026-10-27 · status: pending
What: fix the guard_check.py hype-phrase rule so it matches whole words only and ignores quoted lesson ids (code change - Learner cannot do this).
Why: the HYPE pattern (line 39) has no word boundaries, so the "all-in" alternative also matches ordinary words such as 'fall in', 'call in', 'small inflows', 'overall index', 'all indices' (checked with python re today). Phrase BLOCKs: us.json 21 Oct 20:18, learning.json 23 Oct 19:13 (lesson L-0004 quoted - Governor judged it a self-quote), desk_notes.json 27 Oct 13:48 (lead L2; the text was rewritten before this run, so whether it was real hype or a word like 'fall in' cannot be checked). Governor for_learner 27 Oct counts phrase BLOCKs in 3 of the last 5 sessions.
Evidence: logs/governance_log.csv (3 phrase BLOCKs); scripts/agent/guard_check.py line 39; n=3, at least 1 false.
Exact change: wrap the pattern in word boundaries (e.g. \b(...)\b) and skip text inside a quoted lesson (after 'L-00xx'). Also suggested: guard keeps a copy of a BLOCKed file so the Learner can see the exact phrase.
How to undo: revert guard_check.py to the previous version.

Update 2026-10-27 (learner, P-0003): still happening - whole morning schedule missed on 27 Oct (first run 13:18 IST, 11 of 27 runs, incl. 3 Governor audits); 6th session.
Update 2026-10-27 (learner, P-0005): validation/report.json 27 Oct 18:43 - same tested claims 'not holding' (Bank Nifty 5m Range x1.40 n=38; Bank Nifty 15m Range x1.11 n=21; BTC 4h Range x1.26 n=20; Nifty 5m direction IC -0.036 n=203; Nifty 15m direction IC -0.023 n=103); Nifty 15m Volatile now 'weak' (x1.01, n=54); Nifty 5m Volatile holding (x1.26, n=76). Still 6 sessions, one regime - same advice: wait and re-test.
Update 2026-10-28 (learner, P-0003): still happening - 7th session; Governor for_learner 28 Oct lists N2 hourly, S3, I3, P1/S2/I5, C1/C2 and the morning G1 audits missing; 20 desk runs logged today.
Update 2026-10-28 (learner, P-0005): validation/report.json 28 Oct 18:25 (live since 20 Oct) - 6 tested claims 'not holding': BTC 4h Range x1.21 (n=20); Bank Nifty 5m Range x1.43 (n=38); Bank Nifty 5m Volatile bigger moves x0.87 vs x1.39 backtest (n=22, new); Nifty 15m Volatile x0.96 (n=69, back to not holding); Nifty 15m direction IC -0.086 (n=128); Nifty 5m direction IC -0.039 (n=251). Bank Nifty 15m Range now 'weak' (x0.96, n=24). Holding: Nifty 5m Volatile x1.19 (n=88). Still 7 sessions, one High_Volatility stretch, overlapping bars - same advice: wait and re-test after ~20 live sessions; no label change.

## P-0008 · 2026-10-29 · status: pending
What: make the nifty-live-tracker snapshot.json reachable from scheduled desk runs (folder connection / ops - Learner cannot do this).
Why: intraday I1/I2 ran without prior-day levels, OI, VWAP, ORB or volume profile in 4 runs over 3 sessions; see L-0006. I2 29 Oct also hit HTTP 429 on the trades feed (no CVD/delta).
Evidence: logs/agent_log.jsonl intraday I1 26/28/29 Oct, I2 29 Oct ('Documents/nifty-live-tracker not mounted'); n=4.
Exact change: Nikhil connects the nifty-live-tracker folder (or copies snapshot.json into the platform folder) for scheduled tasks. No prompt or rule change.
How to undo: nothing to undo; retire L-0006 at review once the file is there.

Update 2026-10-29 (learner, P-0003): still happening - 8th session; Governor for_learner 29 Oct: N2 hourly, S3, I3, P1/S2/I5, C1/C2 and G1 morning audits missing; L3 ran 71 min late; 10 of 27 runs (Governor G2).
Update 2026-10-29 (learner, P-0006): repeated - guard BLOCK 29 Oct 08:47 desk_notes.json ideas 1-2 (Nifty 22,950-23,160, Bank Nifty 54,950-55,540 vs ES close 7746.75); Governor counts 'level' misfires on India output 26 and 29 Oct.
Update 2026-10-29 (learner, P-0007): repeated - guard phrase BLOCK 'all in' on intraday.json 29 Oct 10:28 (text fixed before this run, so true vs substring match cannot be checked); 3 of the last 5 sessions per Governor. Interim tightening G-0003 added.
Update 2026-10-29 (learner, P-0005): validation/report.json 29 Oct 19:17 - same 6 tested claims 'not holding' (e.g. Bank Nifty 5m Range x1.43 n=38; Nifty 15m Volatile x0.96 n=69; Nifty 5m direction IC -0.039 n=251); Bank Nifty 15m Range 'weak' (x0.96 n=24); Nifty 5m Volatile holding (x1.19 n=88). Still 8 sessions, one regime - same advice: wait and re-test.

Update 2026-10-30 (learner, P-0004): same problem, now the Dhan token - health.json 'fail' (Dhan token expired 22 h at 19:06) in 9 runs today (US3, N1, S1, I1, L1, L2, I2, N2, L3); 5m/15m/4H bars and regime v2 stale since 28 Oct; I2 29 Oct calls still cannot be graded. New process lesson L-0007. Fix: renew the Dhan token before 08:00 IST (health.json gives the steps).
Update 2026-10-30 (learner, P-0006): repeated - guard 'level' BLOCK 30 Oct 08:47 desk_notes.json idea 1 (Bank Nifty 55,500/55,770/55,900 vs ES close 7676.0); 3rd India misfire (26, 29, 30 Oct) per Governor for_learner.
Update 2026-10-30 (learner, P-0003): still happening - 9th session; Governor for_learner 30 Oct: missed scheduled runs incl. G1 08:55/10:55/12:55; L3 notes no I5, S2, P1 or N3 by 16:54.
Update 2026-10-30 (learner, P-0005): validation/report.json 30 Oct 18:51 - India intraday numbers unchanged from 28-29 Oct (bars stale since 28 Oct, Dhan token), so no new India evidence. New: US NDX 1h 'Volatile -> bigger moves' not holding (backtest x1.51, live x0.59, n=13); BTC 4h Range x1.25 (n=22). Still too early - same advice: wait and re-test after ~20 live sessions; no label change.

## P-0009 · 2026-10-31 · status: pending
What: refresh the US daily history file so US calls on SPY / QQQ can be graded (data / ops fix - Learner cannot do this).
Why: market_data/history/yahoo_us_daily.csv last bar is 2026-10-19, so US2-20261020-1947-1 (SPY) and -2 (QQQ), 3-day no-trade calls, still show "0/3 sessions so far" after 9 US sessions. Ungraded calls count as not having happened. US1 calls (SP500 / NDX) grade fine from yahoo_daily_deep.csv. Minor, same family: 6 crypto C2 intraday no-trade calls (20 Oct, 240-300m) graded 'too_little_history' (0 comparable windows).
Evidence: grade_calls.py daily() reads history/yahoo_us_daily.csv for SPY/QQQ; file tail 2026-10-19 (108,030 rows); learning_scorecard.json waiting list 31 Oct; calls_graded.csv C2-20261020-* (n=6).
Exact change: Nikhil adds yahoo_us_daily.csv to the daily data refresh (or maps SPY -> ES=F and QQQ -> NQ=F in grade_calls.py YDEEP for grading). Optional: give grade_calls.py a crypto intraday baseline source. No prompt or rule change.
How to undo: revert the data job / grade_calls.py mapping.

Update 2026-10-31 (learner, P-0005): validation/report.json 31 Oct 18:51 - US NDX 1h 'Volatile -> bigger moves' back to holding (x1.13, n=22; was x0.59 n=13 on 30 Oct) - shows how fast small samples swing. BTC 4h Range x1.30 (n=25) still not holding. India numbers unchanged since 28 Oct (bars stale, Dhan token). Holding: Nifty 5m Volatile x1.19 (n=88), Nifty 15m Trending down drift (n=17). Same advice: wait and re-test after ~20 live sessions; no label change.
Update 2026-10-31 (learner, P-0004): health still 'fail' (Dhan, India inputs) at US3 07:37 on 31 Oct (guard WARN); India 5m/15m/4H bars stale since 28 Oct; I2 29-30 Oct calls (n=4) cannot be graded until session data is complete.
Update 2026-11-03 (learner, P-0003): still happening - 10th session; no desk runs at all on Mon 2 Nov; 3 Nov: G2 counts 8 of 29 scheduled runs logged, US3/C1 ~90-120 min late (C1: folder access had to be re-granted), L3 16:46 late, no I1/I5/S2/P1 output (logs/agent_log.jsonl, out/governance.json for_learner 3 Nov).
Update 2026-11-03 (learner, P-0004): health.json 3 Nov 19:07 shows Dhan token ok (valid 23 h) and keep-alive running; India bars fresh again from L2 09:49. If it stays ok, this can close; L-0007 stays until its review.
Update 2026-11-03 (learner, P-0005): validation/report.json 3 Nov 18:27 - 6 tested claims 'not holding'. New: BTC 4h 'Volatile -> bigger moves' x1.47 backtest -> x0.48 live (n=11). Others: BTC 4h Range x1.38 (n=25); Bank Nifty 15m Range x1.01 (n=25); Bank Nifty 5m Volatile x0.91 (n=22); Bank Nifty 5m Range x1.46 (n=40); Nifty 15m Volatile x0.89 (n=89). Learner read unchanged: wait for ~20 live sessions and a research re-test; no label change by the Learner.
Update 2026-11-03 (learner, P-0007): 4th phrase BLOCK - news.json 3 Nov 18:08 BLOCKed on the letters 'all in' inside a word (N3 unsure); fixed and PASS at 18:08:36. G-0003 was followed after the BLOCK; the whole-word fix would stop these false BLOCKs.
Update 2026-11-03 (learner, P-0009): US2-20261020-1947-1/-2 (SPY/QQQ) still 0/3 sessions after 2 weeks.

## P-0010 · 2026-11-03 · status: pending
What: let a persona mark a registered call as withdrawn so it is excluded from grading (code change to agent_log.py / grade_calls.py - Learner cannot do this).
Why: I2 registered I2-20261103-1027-1 (Nifty VWAP-pullback debit spread) then withdrew it per L-0006 (tracker snapshot stale) and asked for it to be graded as withdrawn; agent_calls.csv has no such status, so it stays open and was graded 'not_filled'. Governor for_learner 3 Nov raised the same point.
Evidence: logs/agent_log.jsonl intraday I2 3 Nov 10:28 unsure; out/governance.json for_learner 3 Nov; grade_calls.py output 3 Nov 19:11; n=1.
Exact change: add `agent_log.py withdraw --call-id <id> --why "..."` that writes status=withdrawn + time; grade_calls.py skips withdrawn calls (or reports them separately) and the scorecard shows a withdrawn count. No prompt or rule change.
How to undo: revert the two scripts; withdrawn rows grade as before.
