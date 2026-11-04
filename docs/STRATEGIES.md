# Market Pulse – Strategy and Engine Logic

Everything here is **paper trading or context only**. No module places orders. Forward results never change a rule automatically; a person approves every change. Rules were fixed before live results were seen. Each section names the code file that is the source of truth.

Contents: 1 Regime engines · 2 Hourly verdict · 3 Blocks (level map) · 4 Reference levels · 5 Trade setups and guard · 6 Paper rules (4H block break, touch-minute cards, DOM hold / break, momentum basket) · 7 Crypto engine · 8 Descriptive panels · 9 News scoring · 10 Global narrative.

---

## 1. Regime engines

### 1a. Exposure regime – sizing (`src/common/regime_engine/exposure_regime.py`)
Five states, first match wins, decided at the close of day t and used from t+1:

| State | Rule | Exposure (long only) |
|---|---|---|
| High Volatility | realised-vol 1-year percentile > 80, or implied-vol percentile > 90, or (vol pct > 60 and 60-day drawdown < −10 %) | 0.2 |
| Bull Trend | efficiency ratio(20) > 0.30 and ADX(14) > 22.5 and 20-day return > 0 and close > EMA200 | 1.0 |
| Bear Trend | ER(20) > 0.30 and ADX(14) > 22.5 and 20-day return < 0 and close < EMA200 | 0.15 |
| Compression | vol pct < 30 and ER(20) < 0.15 | 0.9 |
| Range | anything else | 0.6 |

Research verdict: regime is useful for **size**, not direction.

### 1b. Per-timeframe regime (`tf_regime.py`)
Same five names on each timeframe's own bars (last ≤ 2,000): High Volatility if vol rank > 80 (or > 60 with a 60-bar drop > 10 %); Bull / Bear Trend if ER(20) > 0.30, ADX(14) > 22.5 and trend score ≥ +3 / ≤ −3; Compression if vol rank < 30 and ER < 0.15; else Range. Trend score (−10…+10) = half price vs 200-bar EMA, half 20-bar momentum + EMA 9/21/50 stack. Understanding only; size still comes from 1a. Paper variants recorded: `dir_score` (direction from trend score) and `confirm_2d` (a label change needs 2 days).

### 1c. Score v2 – direction and character per timeframe (`score_v2.py`, `score_v2_markets.py`)
- Direction −100…+100 = equal-weight mean of signed feature scores × 100. Feature score = 2·Φ(z) − 1, z against the instrument's own rolling ~1 year.
- Trend 0–100 (efficiency ratio + ADX vs own history), Vol 0–100 (realised vol, + India VIX on daily).
- Label Trending up / Trending down / Range / Volatile / Mixed, switched only after it holds K bars.
- India inputs: 5m momentum + distance from VWAP; 15m EMA20 vs EMA50, RSI−50, opening-range position; 75m ADX×DI sign, swing structure, EMA20 slope; 1D close vs EMA50 / EMA200, 20-day return, breadth, India VIX (inverse); 1W 26-week return, close vs 40-week average, US 10Y / USD-INR / crude 4-week change (inverse), S&P 500 13-week return.
- US: 5m, 15m, 1h, 1D, 1W with VIX, VIX3M, US10Y, DXY, peer index. Crypto: 5m, 15m, 4h, 1D, 1W with funding, DVOL, Nasdaq, DXY, taker-buy share.
- Which inputs are trusted is read from `score_v2_validation.json`.
- **Known issue:** the ±10 direction scores failed testing and should be shown as context, not direction.

### 1d. HMM challenger (`hmm_regime.py`)
3-state Gaussian HMM on [daily log return, log 20-day vol]; refit each January on prior data only; forward filter only (no look-ahead); exposure = Σ P(state) × {1.0, 0.6, 0.15}. Challenger, not live.

---

## 2. Hourly verdict (`app/hourverdict.py`)
Direction from the 5-minute and 15-minute rows: `avg = (score5 + score15) / 2`.
- **up** if both trends "Up" and avg > −1, or avg ≥ 3; **down** if both "Down" and avg < 1, or avg ≤ −3; else **mixed**. **Strong** if |avg| ≥ 5.
- Price = spot for Nifty / Bank Nifty, futures for ES, NQ and commodities.
- Blocks from section 3 (Nifty / Bank Nifty) or the dashboard's support / resistance levels (others). "Near" = block mid within a per-market % (default 0.4 %).

| Situation | Verdict |
|---|---|
| Price inside a medium / strong block | At a block – wait |
| Down, near a strong support | Into strong support – wait |
| Down, near resistance | Sell on rise |
| Down otherwise | Trend continuation ↓ (strong) or Sell on rise |
| Up, near a strong resistance | Into strong resistance – wait |
| Up, near support | Buy on dips |
| Up otherwise | Trend continuation ↑ (strong) or Buy on dips |
| Mixed | Sideways – wait |

Full update hourly, re-checked every 15 min. Every reading is logged with price for grading. Record so far: 57 % hit, +5.4 bps, which is below costs. Context only.

---

## 3. Blocks – rolling level map (`app/levelmap.py`; stocks: `app/stockview.py`)
Built from the last 10 sessions in **spot** prices. Weighted inputs:
- this / previous week and month high & low: weight 3 (★ star level);
- previous day high / low: 2; close: 1;
- composite volume profile (Dhan futures 1-min volume moved to spot by each day's basis): 10-day POC 2.5, VAH / VAL 1.5; 5-day POC 2, VAH / VAL 1.5; high-volume nodes 1.5;
- 30-min swing highs / lows, newer swings weigh more (1.0 → 0.5);
- option strikes: OI share × 6 + fresh writing share × 2 + volume share × 2.

Score(price) = sum of weights within ± band (Nifty 15 pts, Bank Nifty 40). Blocks = strongest peaks at least 4 bands apart; edges where the score falls below 80 % of the peak. Strength by score: strong / medium / weak. Evidence: price level + option OI together held 77–80 % of the time; levels alone broke 13–19 %. **Use strong blocks for stops and targets, not entries.**

Stocks (`stockview.py`): same idea, band 0.25 % of price; strong = score ≥ 6 with OI or a star level, medium ≥ 3.5, else weak.

## 4. Reference levels (dashed lines on block charts)
- India (`levelmap.build()["levels"]`, `stockview._ref_levels`): PDH / PDL, PWH / PWL, PMH / PML, 5-day VAH / VAL / POC, IB high / low (09:15–10:15 futures minus basis), VWAP of the last session.
- Crypto, metals, energy (`app/reflevels.py`): UTC days. VAH / VAL = 5-day value area (70 % of volume) from 1-hour bars, price step 0.1 % of spot; IB = 00:00–01:00 UTC (previous day's until today's first hour completes); PDH / PDL previous UTC day; PWH / PWL previous ISO week; PMH / PML previous calendar month (only if > 20 days of bars); VWAP = current UTC day. Sources: Binance USD-M 1h klines; Hyperliquid `xyz` perps 1h candles. Cached 5 min.

---

## 5. Trade setups and the "don't trade" guard

### Setups v1 – Nifty / Bank Nifty (`app/setups.py`)
Every 5 min, 09:30–15:00 for new setups. Six votes summed: trend (5m + 15m score), impact score (trend 40 / flows 30 / positioning 30), options OI build-up / covering (last 15 min), futures OI state, PCR change since open, order-flow 15-min delta of the last 3 bars.
- Score ≥ +3 → **Buy Dips**; ≤ −3 → **Sell Rises**; else **Stand Aside**.
- Entry = nearest level on the dip (rise) side; stop beyond the next level (0.15–0.6 % of price); targets = next two levels the other way; needs reward:risk ≥ 1.2 to be "ready".
- Life: watching → active → target 2 / stop / breakeven after target 1 / time exit 15:20; cancelled if the read flips before entry; expires 15:00.
- Graded in points and R, also net of 4 pts (Nifty) / 10 pts (Bank Nifty) option cost.

### Guard (`app/prep.py`)
| Condition | Rule |
|---|---|
| Budget / election-result day | Stand aside all day |
| RBI policy day | No new entries 09:55–10:35 |
| Opening gap > 0.6 × average day range | Half size, no entry before 10:15 |
| 15-min label "Volatile" | Half size |
| Shock mode active | No new setups |

Shown as context only (failed tests): FII / Pro / Client positions for direction after the open; overnight US 10Y, USD-INR and crude moves.

---

## 6. Paper rules running

### 6a. 4-hour block break (`app/break4h.py`) – BTC, ETH, SOL, Gold
- **Map:** blocks from the last 30 days — previous week / month high-low, previous day high / low / close, 30-day volume profile (POC, VAH, VAL, high-volume nodes), daily swing highs / lows, round numbers; density peaks with band 0.4 %. Built only from data before the day.
- **Signal:** a 4H bar enters a block from outside **and** trades beyond its far edge in the same bar. Crypto also needs taker volume in the break direction, net ≥ 5 % of the bar's volume.
- **Trade:** enter at the bar close with the break; stop just back past the near edge (+0.04 %); exit after 12 bars (2 days).
- **Grading:** R and bps after 0.14 % round-trip cost. Expect a 20–30 % win rate and long losing streaks.

### 6b. Touch-minute option cards (`app/touchtrades.py`, `app/touchlog.py`) – Nifty / Bank Nifty
- Touch log records every key-level touch 09:30–14:30 IST (yesterday's POC / VAH / VAL, PDH / PDL, prior week / month, OI walls, max pain) with footprint and outcome.
- **Signal:** the touch minute closes with buyers in control at support (delta / volume > +0.2) or sellers in control at resistance (< −0.2).
- **Trade:** enter at that minute's close in the bounce direction; exit 30 min later at market; no stop, no target. One open trade per index; same-minute merged levels count once.
- **Vehicle (from 4 Nov 2026):** buy the ~0.6-delta option (call for long, put for short), next-week expiry (7+ days left; Bank Nifty: nearest monthly with 7+ days), 1 lot. Stop on spot 1-minute closes 0.10 % past the level; +1R → breakeven; +2R → trail 1R; out after 30 min.
- **Grading:** gross bps and points on spot first; net = option round trip: real charges (STT 0.15 % of sell, exchange, GST, stamp, ₹40 brokerage) + assumed spread 0.5 pt (Nifty) / 1.5 pt (Bank Nifty) a side. In index points that is ~5.5 / ~15 pts. Lots: Nifty 65, Bank Nifty 30. Status: hypothesis, not a tested edge.

### 6c. DOM hold / break (`app/domcards.py`) – Nifty / Bank Nifty futures + 48 liquid F&O stocks
- **Levels** fixed for the day on first read: index = medium / strong blocks (shifted to futures by the live basis) + call / put OI walls; stocks = yesterday's high / low / close; plus opening range 09:15–09:30 (stocks) / initial balance 09:15–10:15 (index).
- **HOLD:** price comes into a level zone from away (outside 2+ min), does not break more than 2 bands past the far edge, and leaves on the side it came from within 2 min; book leaning our way over the last 30 s (20-level imbalance ≥ 0.10 above its 30-min median); aggressive side absorbed (index: market sells ≥ market buys in the zone; stocks: bid qty refilled). Trade the bounce; stop 2 bands beyond the far edge.
- **BREAK:** price leaves the zone on the far side and stays beyond for 30 s; book leaning the break way (≥ 0.15 vs median); index: 60-s market-order delta ≥ 15 % of volume in the break direction; stocks: defending side's 20-level qty down ≥ 30 % vs 2 min earlier. Trade the break; stop 1 band inside the near side.
- **Both:** target = next level, at least 2R (else 2R); out after 60 min or 15:20; new trades 09:30–15:00; one open trade per market; each level / setup / side once a day; max 4 trades per market a day; fill at mid.
- **Costs:** index = 0.6-delta next-week option round trip in index points: Nifty 5.5, Bank Nifty 15 (charges + spread + up to 60 min decay ÷ 0.6 × lot); stocks 0.08 %. ₹1,000 risked per trade. "Plan account": first 3 trades a day, stop for the day at −2R.
- **Judged** after 30–40 sessions: must be positive after costs in both halves with 30+ trades. Day 1: 84 trades, +0.04R gross, −0.14R net.

### 6d. Cross-section momentum basket (`app/xsecpaper.py`) – crypto
- Universe: 29 Binance USDT perps (list in `data_store/research/alts1m/established.txt`, kept in the repo as config).
- Daily at 00:00 UTC: rank by 72-hour return; long the 5 best, short the 5 worst, equal weight; hold 24 h.
- P&L per sleeve = (mean long return − mean short return) / 2; cost 0.10 % per rebuild. **Known issue:** a basket got stuck open.

---

## 7. Crypto engine (`app/cryptoengine/`, started by `app/crypto_live.py`)
Hyperliquid (+ Binance) websockets → 5-min candles, footprint, CVD, levels → setups → paper book. Also used by the metals tracker (same code base).
- **Stance** (`analytics/regime.py`): trend up → "Buy dips"; trend down → "Sell rallies"; range → "Fade edges" around POC; mixed → "Stand aside".
- **Reversal setup grade** (`analytics/setup.py`): needs a reversal at a level; + Tier-1 level 2 (Tier-2 1); + strong absorption 1; + divergence agrees 2 (opposes −1); + continuation with regime 1; + fade confirmed by change of character 2. Points → A+ / A / B / C; state "ready" or "watch".
- **Signals** (`analytics/signals.py`): surfaced only when ≥ `min_confluence` factors agree; perp-native factors (funding as contrarian crowd read, OI / funding flow, participation).
- **Plan** (`analytics/trade_plan.py`): entry, stop just beyond the invalidating structural level (clamped to a volatility floor / ceiling), targets = next structural levels, R:R on target 1.
- **Paper book** (`paper_trade.py`): opens at score ≥ `paper.min_score` with a valid plan; target = win (+R), stop = loss (−1R); no session-end flatten for crypto.

## 8. Descriptive panels (not signals)
- **Block watch** (`blockwatch.py`): 1-minute zoom at the nearest zone (within 0.35 % crypto / 0.25 % Nifty). Status: approaching → testing → rejection forming → rejected, or breaking → broke out. Fading rejections lost money after costs, so it is a description only.
- **OI writer monitor** (`oiwatch.py`): strikes within ±2 %; put writers adding / leaving, call writers adding / leaving, new top-3 wall. Untested.
- **Stock view** (`stockview.py`): per timeframe score /10 from 4 checks × 2.5 (Up ≥ +5, Down ≤ −5); regime from 20-day vol percentile + trend; untested on stocks.

---

## 9. News scoring

### Headlines and shock mode (`app/news.py`)
- **Market shock:** Nifty / Bank Nifty ≥ 1 % in ~15 min or ≥ 2 % on the day, or India VIX +10 % on the day.
- **Severity by keyword tiers:** 3 = shock (war, attack, surprise policy, crash, circuit, sanctions, default, ban); 2 = scheduled market-moving (RBI, Fed, CPI, GDP, budget, big-stock results); 1 = normal. A noise filter drops irrelevant items.
- **Shock mode:** a severity-3 headline from ≥ 2 sources, or a market shock → active for 3 hours; the desk says "no new positions until the regime updates".
- **Tone** per headline −2…+2 from positive / negative word lists; oil-price headlines are inverted (falling oil = positive for India); "lower circuit / 52-week low" −2, "upper circuit / 52-week high" +1.
- **Sentiment score:** weight = severity × min(sources, 3) × 0.5^(age_hours / 12), last 48 h; score = 5 × weighted mean tone → −10…+10 for All, India and Global. Label: Positive ≥ 2, Negative ≤ −2, else Mixed.

### Tab catalysts (`app/tabnews.py`)
Per tab (India, US, Crypto, Metals, Commodities): keep only headlines that **name something on that tab** and carry a **known price catalyst** (results beat / miss, order win, deal, rating change, regulator action, guidance, management exit, ETF flows, hack, OPEC, inventories, supply disruption). Opinion pieces ("should you buy…", "price prediction") are dropped. Direction up / down / either, with "shares rise / fall" reaction words overriding; for commodities and crypto the asset's own move sets direction. Strength = catalyst weight × sources × recency. Near-duplicates removed by token overlap. Max 2 items per name, 10 per tab, last 48 h. Rebuilt every 10 min.

## 10. Global narrative (`app/narrative.py`)
- **Facts** (no AI, 30-min cache): for Nifty 50, S&P 500, Nasdaq 100, Dow, Euro Stoxx 50, Nikkei 225, Kospi, Hang Seng, Shanghai — 1-day / 1-week / 1-month change; "big week" if |weekly move| ≥ 2 × its 1-year weekly standard deviation; turning points in the last 5 sessions (crossed 50 / 200-day average, new 52-week high / low, first close 10 % or 20 % below the 52-week high). Cross-asset 1-week moves: gold, Brent, dollar index, US 10Y, US VIX, Bitcoin. Macro readings at the 90th / 10th percentile of the year or |z| ≥ 2. Rolling 7-day log of severity 2–3 headlines.
- **Story:** written daily by the scheduled task (see SCHEDULED_TASKS.md) into `data_store/agent/out/global_narrative.json`; shown if under 36 hours old; checked by `scripts/agent/narrative_check.py`.
