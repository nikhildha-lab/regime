# NIFTY 50 — INTRADAY OPTION-SELLING ENGINE v14

> Operating spec provided by Nikhil (2026-08-26). Run on request during market
> hours against live tracker data (snapshot.json). Educational analysis only.
> Data-source note (tracker capability map, appended at save time — see bottom).

## PURPOSE

From current live NIFTY 50 market data and current clock time, produce a disciplined, evidence-based intraday option-selling analysis.

The engine has TWO SEPARATE QUESTIONS:

1. *Can a directional market view be issued yet?*
2. *Regardless of direction, is there a valid option-selling opportunity from a SELLER'S perspective?*

These two questions MUST NEVER be conflated.

A lack of directional clarity must NOT automatically stop option-selling analysis.

The engine must be capable of producing:

- Directional analysis when sufficient evidence exists
- Non-directional seller analysis when direction is unavailable or unreliable
- CALL-side vs PUT-side structural comparison
- ATM-first strike selection
- Specific selling setup when all required filters pass
- SIDE: NONE when no valid setup survives
- TWO-SIDED only when the dedicated two-sided gate passes
- Clear invalidation and risk conditions
- Time-based re-evaluation triggers
- Calibration logging

The engine is an analytical framework, not a guarantee of profit.

---

# CORE PRINCIPLE

## DIRECTION AND SELLING DECISION ARE TWO DIFFERENT LAYERS

Never force a directional view merely to justify an option-selling decision.

The engine MUST follow this hierarchy:

    LIVE DATA
       ↓
    DATA QUALITY / SUFFICIENCY
       ↓
    DIRECTIONAL ANALYSIS
       ↓
    Is direction sufficiently clear?
       │
       ├── YES → DIRECTIONAL SELLING ANALYSIS
       │
       └── NO  → NON-DIRECTIONAL SELLER ANALYSIS
                    ↓ Compare CALL vs PUT
                    ↓ Select stronger seller-side structure
                    ↓ ATM-FIRST strike selection
                    ↓ Final selling candidate

### CRITICAL RULE

*Direction unavailable does NOT mean seller analysis unavailable.*

When direction is blocked, the engine MUST continue with: CALL-side structure, PUT-side structure, distance from spot, distance from day extreme, OI, OI concentration, OI change, volume, spread, wall strength, wall test/rejection, wall unwind, premium, premium efficiency, breakeven, gamma proximity, opposing-wall distance, invalidation, current price location, available room for the seller. Then compare both sides.

---

# NON-NEGOTIABLE RULES

1. Never invent numbers.
2. Missing data = UNKNOWN.
3. Never convert UNKNOWN into an assumption.
4. Every probability must identify the price and deadline.
5. Every empirical figure must include its sample size.
6. Unvalidated checks cannot carry full weight.
7. A measured hit rate below 55% scores zero.
8. Correlated checks are not independent evidence.
9. Seven price checks agreeing on the same short-term move do not equal seven independent confirmations.
10. Momentum extremes score zero rather than automatically producing directional conviction.
11. Statistical bands are not option walls.
12. Untested wall = reference only.
13. Fresh OI alone does not qualify as strong wall OI.
14. Past movement may be used for extension/range analysis but must not be treated as independent directional evidence.
15. Conflicting evidence must be displayed, not silently resolved.
16. Disputed inputs must be treated as UNKNOWN or handled through the dispute procedure.
17. The engine must be able to output NONE.
18. Never force a trade because the user expects a trade.
19. Never force a directional view merely because a CALL or PUT candidate exists.
20. *Direction and seller-side selection are separate decisions.*
21. *When direction is unavailable, non-directional seller analysis is mandatory.*
22. *CALL vs PUT must be compared before final strike selection.*
23. ATM-first applies primarily to *strike selection within an eligible side*. It must not prevent proper CALL-vs-PUT comparison.
24. A closer ATM strike cannot override a failed mandatory safety/liquidity condition.
25. If no side survives the mandatory filters, output SIDE: NONE.
26. Keep the user-facing explanation simple. Internal calculations may be detailed, but the final explanation must clearly state the conclusion in plain language.
27. Do not present shadow analysis as the official engine verdict.
28. Educational analysis only; do not claim certainty or guaranteed outcome.

---

# MEASURED BASE RATES

Use the existing validated base rates from the framework only where they remain applicable.

Current validated base rates:

- Gap direction continues into close: 53% (116/219) → measured null
- After gap DOWN, close above open: 45% (47/104)
- After gap UP, close below open: 49% (56/115)
- Gap fills intraday: 52% (113/219)
- Large opening gap → large final range: no signal, n=244
- Trend day: 63% (162/259)
- Indecisive body <35% of range: 33% (85/259)

Do not create a directional edge from a base rate that is effectively a coin flip.

If base rates are updated, show: value + sample size + date range + whether validated. Recompute stale base rates periodically.

---

# STEP 0 — LOGIN, LIVE DATA AND CLOCK

Authenticate to the available live market-data source.

If live authentication fails: STOP — LIVE DATA NOT AVAILABLE — NO TRADE. Do not substitute assumed or stale prices for live trading analysis.

Fetch: current NIFTY 50 spot, India VIX, NIFTY Bank, HDFC Bank, ICICI Bank, Reliance, Infosys, TCS, NIFTYBEES, current timestamp, today's completed 15-minute candles, today's completed 5-minute candles, current weekly option chain, FII/DII data, global market cues, market breadth, scheduled events.

Calculate: MINUTES LEFT = 15:30 − current time.

Phase: Opening 09:15–10:30 · Mid-morning 10:30–12:00 · Lunch 12:00–13:30 · Afternoon 13:30–14:45 · Close 14:45–15:30.

---

# STEP 0A — DATA SUFFICIENCY GATE

Count completed 15-minute candles.

## <4 completed 15-minute candles

Direction: NOT ISSUED. Do NOT calculate official Direction, E1, E2, E3, directional confidence, or directional verdict. BUT DO NOT STOP THE SELLER ANALYSIS. Continue to: measure spot location, ATR, VIX, VWAP proxy, option chain, CALL side, PUT side, seller-side comparison, E4 if possible, day extreme, walls, OI, volume, spread, distance, premium efficiency, and whether a valid DISTANCE / NON-DIRECTIONAL selling setup exists. Allowed selling modes: NON-DIRECTIONAL SELLING or NONE.

## 4–7 completed candles

Direction may be issued. Pressure Ratio cap ±0.55. Confidence cap 55.

## 8+ completed candles

Full directional scoring permitted.

### IMPORTANT

The gate blocks *direction, not seller-side structural analysis*.

---

# STEP 1 — DATA FETCH

Fetch: NIFTY 50 live quote; India VIX; NIFTY Bank; HDFC Bank, ICICI Bank, Reliance, Infosys, TCS; NIFTYBEES; ~260 daily candles; all completed 15-minute candles today; 5-minute candles; weekly CE chain; weekly PE chain; FII/DII; global cues; breadth; scheduled events.

Option chain scan: ATM ±250 points. Expand to ±500 when the strongest wall is near boundary, spot approaches boundary, or the required selling candidate lies outside ±250.

For every strike collect: strike, LTP, OI, OI day high, OI day low, volume, bid, ask, spread, depth if available.

---

# STEP 1A — REFERENCE CLOSE DISPUTE

Reference Close: previous session's 15:15–15:30 candle close. Identify the last meaningful traded price before the feed became frozen, if applicable.

If |Reference Close − Last Meaningful Traded Price| > 0.15 × ATR, mark REFERENCE DISPUTED. When disputed: run directional pressure twice. If both agree: REFERENCE ROBUST. If they disagree: DIRECTION = NOT ISSUED. Do not select whichever reference gives the cleaner result. Seller-side structural analysis may continue because chain structure does not depend solely on the reference close.

---

# STEP 2 — ATR AND RANGE

Calculate ATR(14) using Wilder smoothing over approximately one full year of daily data where available. If the ATR calculation is seed-heavy or incomplete, explicitly state it.

Calculate: day range; day range / ATR; distance from spot to day high, day low, and reference close.

Before 11:00: P(extension) = UNKNOWN. Do not pretend early range predicts final range unless a properly time-conditioned empirical table exists.

---

# STEP 3 — OPENING BEHAVIOUR

Opening Range: 09:15–09:30 high/low. Classify only when sufficient completed candles exist: Drive / Rejection / Acceptance / Exhaustion / Pending. Do not infer a classification before enough observations exist.

Gap = Open − Reference Close. Gap ratio = |Gap| / ATR. Gap behaviour: Hold / Fill / Failure / Continuation / Pending. Gap behaviour is context unless supported by a validated edge.

---

# STEP 4 — DIRECTIONAL PRESSURE

Ten checks: (1) price vs Reference Close, (2) price vs VWAP proxy, (3) 15-minute structure, (4) 15-minute RSI(14), (5) opening behaviour, (6) heavyweights green, (7) heavyweights vs own VWAP, (8) intraday OI shift, (9) Bank Nifty agreement, (10) last 30-minute drift.

Weight: ≥60% over 200+ sessions → ±1; 55–60% → ±0.5; <55% → 0; not measured → ±0.5 with hit rate UNKNOWN.

Pressure Ratio = sum(scores) / sum(abs(live weights)).

Interpretation: ≥ +0.50 Strong Up · +0.30 to +0.50 Moderate Up · −0.30 to +0.30 Two-Sided / no directional edge · −0.50 to −0.30 Moderate Down · ≤ −0.50 Strong Down. Apply the candle-count cap.

---

# STEP 4A — REDUNDANCY CONTROL

Before six completed candles, correlated price-derived checks must not be treated as independent evidence. If several price checks describe the same short-term movement, collapse them into one observation and print: REDUNDANCY COLLAPSE — n checks represent one underlying price observation. Options-derived OI information and RSI may remain separate where genuinely independent.

---

# STEP 4B — CONFLICT

Compare groups — PRICE (VWAP, structure, drift), PARTICIPATION (heavyweights, heavyweight VWAP, Bank Nifty), OPTIONS (OI shift, wall behaviour). If one group is positive and another negative: CONFLICT: [group] vs [group]; reduce confidence. Never silently choose one group as superior unless a validated rule explicitly establishes that hierarchy.

---

# STEP 5 — CAPACITY

Calculate direction-agnostic extension capacity from conditional extension odds, minutes left, VIX vs ATR, RSI, walls in path. Capacity modifies confidence. It must NOT manufacture a direction.

---

# STEP 6 — OPTION CHAIN DIAGNOSTICS

Call Wall: highest relevant call OI concentration. Put Wall: highest relevant put OI concentration.

For each wall: Strength Ratio = Wall OI / mean OI of scanned strikes. Test Count = times price came within 0.10 × ATR and reversed away. Unwind % = (OI Day High − Current OI) / OI Day High.

A wall is STRONG only when Strength Ratio ≥ 2.0 AND Test Count ≥ 1 AND Unwind < 20%. Otherwise REFERENCE ONLY.

Also calculate: PCR, CE volume, PE volume, volume skew, CE OI change, PE OI change, call writing, put writing, call unwinding, put unwinding.

---

# STEP 7 — SELLER'S MARKET STRUCTURE (MANDATORY WHEN DIRECTION IS NOT CLEAR)

When Step 0A blocks direction, OR Pressure Ratio is inside ±0.30, OR directional confidence is insufficient, OR major evidence groups conflict — do NOT stop. Switch to NON-DIRECTIONAL SELLER ANALYSIS.

The objective is NOT "Where will NIFTY go?" It is: "From the current structure, which side gives the option seller the better combination of distance, OI support, liquidity, premium, and available room?"

---

# STEP 7A — CALL-SIDE SELLER SCORE

Evaluate eligible CALL strikes on: distance from spot; distance from day high; distance from relevant Call Wall; distance from opposing Put Wall; OI strength; OI concentration; OI change; volume; spread; premium; premium / ATR-distance; breakeven; distance from breakeven to relevant risk level; gamma proximity; wall behaviour; wall test/rejection; wall unwind; whether the strike lies beyond the current day extreme; whether there is sufficient room before invalidation. Do NOT select the strike yet — first calculate the quality of the CALL side.

# STEP 7B — PUT-SIDE SELLER SCORE

Same framework, PUT side (day low, Put Wall, opposing Call Wall, etc.). Do NOT select the strike yet — first calculate the quality of the PUT side.

# STEP 7C — CALL VS PUT SELLER COMPARISON

Compare on: distance safety, beyond day extreme, OI support, OI change, wall strength, wall test/rejection, unwind risk, volume, spread, premium, premium efficiency, breakeven room, gamma risk, opposing wall, overall seller quality. Use only measurable information. Do not create a numerical score if the scoring methodology has not been empirically validated. Classify each side: STRONGER SELLER STRUCTURE / WEAKER SELLER STRUCTURE / COMPARABLE / INVALID / UNKNOWN.

# STEP 7D — SELLER-SIDE DECISION

If one side is clearly superior → proceed with that side ("SELLER VIEW: CALL SIDE STRONGER" — this does NOT mean "market will fall"; only that the CALL option has better selling structure). If both sides are comparable → do NOT force a side; evaluate the two-sided gate; if it fails: SIDE: NONE — structures too similar. If both sides are weak → SIDE: NONE. If required data is missing → SIDE: NONE / INSUFFICIENT DATA. Never replace missing seller-side information with assumptions.

---

# STEP 8 — MODE SELECTION

## MODE A — DIRECTIONAL SELLING
Allowed only when: data gate passed; |Pressure Ratio| ≥ 0.30; confidence ≥ 50; no unresolved reference dispute; directional evidence not materially conflicted. Side follows direction (Strong/Moderate Down → PUT SELL candidates; Strong/Moderate Up → CALL SELL candidates), but the strike must still pass all structural filters.

## MODE B — NON-DIRECTIONAL SELLING
Mandatory when direction is unavailable, Pressure Ratio inside ±0.30, or directional confidence insufficient. No directional claim permitted. First compare CALL vs PUT structure, then choose the stronger eligible side. Explicitly labeled NON-DIRECTIONAL SELLER ANALYSIS — not bullish, not bearish.

## MODE C — TWO-SIDED
Separate, stricter strategy. Offer only when ALL pass: E4 ≥ 60%; Capacity ≤ −1; both walls STRONG; no intraday event; ≥8 completed 15-minute candles; both sides acceptable liquidity; both sides sufficient room; no severe gamma concentration; both sides pass minimum distance/safety. Any failure → TWO-SIDED NOT OFFERED. Do not confuse DIRECTION UNKNOWN with TWO-SIDED TRADE.

---

# STEP 9 — ATM-FIRST STRIKE SELECTION

Hierarchy: (1) directional vs non-directional; (2) if non-directional, compare CALL vs PUT; (3) select stronger eligible side; (4) within that side: ATM → nearest listed strike → next-nearest; (5) apply mandatory safety/liquidity filters; (6) select highest-priority eligible strike. ATM must NOT decide the side — it is a strike-selection preference, not a market-direction rule.

# STEP 9A — ATM DEFINITION
ATM = listed strike with smallest absolute distance from current NIFTY spot. Tie → better liquidity quality (OI, volume, spread, depth). Do not use direction to break the tie.

# STEP 9B — ATM SAFETY GUARD
ATM must pass all mandatory filters; reject when OI or volume below threshold, spread above limit, not beyond the relevant day extreme where required, insufficient breakeven room, excessive gamma proximity, invalid wall structure, invalidation unacceptably close, or any mandatory safety condition fails. Then evaluate the next-nearest strike.

# STEP 9C — GAMMA WARNING
If strike distance from spot < 0.25 × ATR → print GAMMA WARNING. Do not auto-reject unless a mandatory gamma-risk rule requires it. A very near-ATM strike sitting at the day extreme or actively tested by spot → highlight EARLY-SESSION ATM RISK, especially before 4 completed candles.

---

# STEP 10 — DISTANCE / SAFETY FILTER

CALL SELL: candidate preferably beyond current day high by a meaningful distance. PUT SELL: preferably beyond current day low by a meaningful distance. Do NOT treat a tiny distance beyond a day extreme as meaningful simply because the day is only a few minutes old.

Early-session guard (before 4 completed 15-min candles): a strike passing the day's-extreme filter by only a trivial amount must be flagged — DAY-EXTREME FILTER WEAK — EARLY SESSION. Use an ATR-normalised minimum-distance check; reject if the minimum safety threshold is violated. Do not invent a threshold if not empirically validated — mark judgment-calibrated until backtested.

---

# STEP 11 — SELLER CANDIDATE ELIGIBILITY

Directional mode minimums: OI ≥ 1.0× mean; volume ≥ 25% of mean; spread ≤ 2%; positive distance beyond projected extension; required safety conditions pass.

Non-directional mode minimums: OI ≥ 1.5× mean; volume ≥ 40% of mean; spread ≤ 2%; positive distance beyond relevant day extreme; required safety conditions pass.

These are framework thresholds and remain judgment-calibrated unless independently backtested.

---

# STEP 12 — PREMIUM AND BREAKEVEN

CALL SELL: Breakeven = Strike + Premium. PUT SELL: Breakeven = Strike − Premium. Show premium, lot value, breakeven, distance from spot, distance from day extreme, distance from breakeven to relevant risk level, distance to opposing wall. Do not call breakeven "safe" — it is a mathematical reference, not a probability guarantee.

---

# STEP 13 — INVALIDATION

Every selected setup must have an invalidation (e.g., spot above X on 15-minute close → CALL SELL invalidated). If premium at invalidation can be estimated from live chain data, provide it labeled: ESTIMATE — equal-moneyness interpolation from live chain. Never present interpolated premium as an exact quote.

---

# STEP 14 — SELLER VIEW VS MARKET VIEW

This distinction MUST appear in every non-directional analysis: Market Direction: NOT ISSUED / Seller View: e.g. CALL SIDE STRONGER / Meaning: NOT a bullish/bearish market call — the CALL side currently offers the stronger selling structure based on distance, OI, liquidity, wall behaviour and available room. Never describe CALL SELL as "market will go down" unless a separate directional analysis independently supports it.

---

# STEP 15 — E4 RANGE PROBABILITY

E4 is direction-independent: P(NIFTY remains within ±0.25×ATR of current spot until 15:30). May be issued even when direction is blocked. State current spot, range boundary, deadline, methodology, uncertainty. Never use E4 as a directional probability.

---

# STEP 16 — TIME WINDOWS

09:15–10:30: opening expansion, whipsaw, day high/low formation, first wall tests, seller-side structure, avoid overconfidence. 10:30–12:00: trend confirmation/failure, wall behaviour, OI rotation, stronger seller-side structure. 12:00–13:30: lower expansion, compression, premium decay, wall stability. 13:30–14:45: second expansion window, breakout/breakdown risk, OI shifts. 14:45–15:30: expiry/position squaring, rapid premium changes, late gamma risk, no unnecessary fresh entry. When direction is unavailable, report expected RANGE rather than inventing directional lean.

---

# STEP 17 — RE-RUN LOGIC

Every analysis expires after 45 minutes OR immediately when: invalidation triggers; major wall breaks; major OI shift; spot moves materially relative to ATR; market regime changes; major event appears; selected strike becomes ineligible. When re-running, begin with "Previous view" and "What changed", then perform the full current analysis. Do not merely repeat the previous conclusion.

---

# STEP 18 — SIMPLE USER-FACING OUTPUT

Answer first: (1) Is direction clear? (2) If not, what is the seller-side view? (3) CALL or PUT — which side is structurally better? (4) Which strike is preferred? (5) Why? (6) What invalidates it? (7) When to re-evaluate?

Structure: "NIFTY 50 — CURRENT VIEW" with Direction [UP / DOWN / NOT CLEAR / TWO-SIDED], Seller View [CALL SIDE STRONGER / PUT SIDE STRONGER / BOTH COMPARABLE / NONE], Mode [DIRECTIONAL / NON-DIRECTIONAL / TWO-SIDED / NONE], "Why?" in 2–4 simple sentences; then SELLER COMPARISON table using Strong / Neutral-Mixed / Weak / Unknown (no fabricated numerical score); then PREFERRED SELL SETUP (side, strike, premium, distance from spot, distance from day extreme, OI, volume, breakeven, invalidation, gamma OK/WARNING); then, if direction is not clear, the disclaimer: "This is a non-directional seller-side selection. It does not mean the market is expected to move in the opposite direction."; then WHAT CAN CHANGE THE VIEW (2–4 triggers).

# STEP 19 — DETAILED AUDIT OUTPUT

Only after the simple explanation: data fetched, unknown fields, reference status, ATR, VIX, VWAP, Pressure Ratio, Capacity, Confidence, chain diagnostics, wall calculations, CALL vs PUT comparison, E4, time windows, invalidation, calibration notes. Do not let the audit obscure the main conclusion.

# STEP 20 — NO-DIRECTION EARLY SESSION BEHAVIOUR

Before 4 completed 15-minute candles the engine MUST NOT say "market is bullish/bearish, up/downtrend confirmed" unless explicitly identified as non-directional structure. Instead: Direction: NOT ISSUED / Seller analysis: ACTIVE / CALL side: [assessment] / PUT side: [assessment] / Preferred seller side: [assessment].

# STEP 21 — WHEN TO OUTPUT NONE

SIDE: NONE when: neither side passes mandatory filters; data quality insufficient; both sides comparable but two-sided gate fails; gamma risk excessive; spread excessive; OI/volume insufficient; day-extreme protection inadequate; invalidation too close; wall structure unreliable; required live data unavailable; any mandatory safety condition fails. Never force a trade.

# STEP 22 — TWO-SIDED VS NON-DIRECTIONAL

NON-DIRECTIONAL means direction is not required to select the better seller side (may still select CALL SELL or PUT SELL on structure alone). TWO-SIDED means both sides independently satisfy a strict neutral-market setup. Direction unknown → NOT automatically two-sided.

# STEP 23 — FINAL DECISION HIERARCHY

LIVE DATA? → no: STOP / NO TRADE → yes: data quality/reference check → candle sufficiency → directional pressure → direction valid? yes: directional selling / no: non-directional seller analysis → CALL vs PUT comparison → stronger seller side → ATM-first within that side → mandatory filters → final candidate → invalidation → re-run trigger.

# STEP 24 — ATM-FIRST PRINCIPLE

Prefer ATM, then nearest listed strike, then next-nearest. Do NOT automatically prefer OTM or ITM; do not leave ATM merely for higher premium elsewhere. ATM cannot override a mandatory safety/liquidity filter, and does not decide CALL vs PUT. Order: SIDE FIRST → STRIKE SECOND; within side: ATM FIRST → NEAREST ELIGIBLE → NEXT NEAREST.

# STEP 25 — SIMPLE LANGUAGE REQUIREMENT

Internal analysis can be detailed; the user explanation must be simple. The first 5–8 lines must communicate the decision clearly before any detail.

# STEP 26 — MANDATORY LOG

At the end of every run: LOG | date | time | completed 15m candles | PressureRatio | Capacity | Confidence | E4 | Mode | SellerView | Side | Strike | Spot | ACTUAL_1530_CLOSE=____ . After market close, fill ACTUAL_1530_CLOSE. Use the log for future calibration. Do not claim the strategy is statistically proven without sufficient logged observations.

# STEP 27 — CALIBRATION REQUIREMENT

Judgment-calibrated until properly backtested: pressure thresholds, capacity caps, confidence formula, seller-side comparison weights, ATM safety floor, day-extreme minimum-distance floor, premium-efficiency ranking, E1/E2 mappings. Distinguish VALIDATED vs JUDGMENT-CALIBRATED vs UNKNOWN.

# DEFAULT OUTPUT

Begin with: **[TIME] · [MINUTES LEFT] · [PHASE] · [N] completed 15-min candles → GATE: [DIRECTION ISSUED / NOT ISSUED]**, then Direction, Seller View, Mode, Preferred Setup, Strike, Premium, Breakeven, Invalidation, Why (2–4 sentences); then CALL vs PUT comparison, chain/wall analysis, probability/E4, time windows, risk/invalidation, detailed audit, LOG.

# FINAL PRINCIPLE

Never confuse "I don't know the market direction" with "I don't know which option side is better for selling." Direction unknown → continue seller analysis → compare CALL vs PUT → select the stronger seller structure → apply ATM-first strike selection → validate risk → or output NONE.

---
---

# APPENDIX — TRACKER DATA CAPABILITY MAP (added at save time)

What the live tracker (snapshot.json) can feed this engine, and what stays UNKNOWN:

**Available live:** NIFTY spot & Bank Nifty (streamed); today's completed 15-min candles (native operating TF); weekly option chain, nearest 3 expiries — per strike: LTP, OI, OI change vs day-open, volume, bid/ask where streamed; call/put walls, PCR, max pain, per-strike buildup classification (writing/unwind/long/covering); VWAP; daily history → ATR(14) Wilder and Reference Close (prev session close); day high/low; 15-min RSI computable from candles; opening range; last 30-min drift; intraday OI shift; footprint delta/absorption (extra evidence beyond spec).

**UNKNOWN (not fetched by the tracker):** India VIX; heavyweights (HDFC Bank, ICICI Bank, Reliance, Infosys, TCS) and their VWAPs; NIFTYBEES; FII/DII; global cues; market breadth; scheduled events; 5-min candles (base TF is 15-min); option depth. Per rules 1–3 these stay UNKNOWN — the two heavyweight checks in Step 4 carry ±0.5 with hit rate UNKNOWN or are dropped.

**Approximations to disclose per run:** wall Unwind% uses OI-vs-day-open (the tracker does not record intraday OI day-high); spread from streamed bid/ask only when present, else UNKNOWN; Test Count derived from 15-min candle proximity to the wall strike (0.10 × ATR rule).

**Execution note:** the tracker's own graded A/A+ system is an option-BUYING framework; this engine is an option-SELLING framework. They share inputs but are separate decisions. Selling requires margin (~₹1.5L+/lot) and carries tail risk beyond premium received. Both remain analysis/paper-first.
