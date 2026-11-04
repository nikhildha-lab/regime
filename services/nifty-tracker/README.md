# Nifty / Bank Nifty — Live Intraday 5-min Tracker

A real-time intraday dashboard + alert engine for **NIFTY** and **BANK NIFTY** that
reads market structure live from **price action, volume profile, market profile
(TPO), open interest, and order-flow imbalance**, then fuses them into
**confluence-scored trading opportunities**.

It streams from **Zerodha Kite Connect**, builds 5-minute candles on the fly, and
serves a browser dashboard plus Telegram / desktop alerts. It also writes a
`snapshot.json` every cycle so an LLM analyst (e.g. the
[`indian-trading-skills`](https://github.com/ajeeshworkspace/indian-trading-skills)
F&O planner) can reason over the live read-out.

> ⚠️ **Not investment advice.** This is an analysis tool. It does **not** place
> orders. Signals are structural observations, not recommendations.

---

## What it computes

| Family | Signals produced | Built from |
|---|---|---|
| **Price action** | swing structure, BOS / CHoCH, trend (HH-HL / LH-LL), opening range + breakout, VWAP, swing-cluster support/resistance | **index** 5-min candles |
| **Volume profile** | POC, value area (VAH / VAL), HVN / LVN, price-vs-value acceptance | **futures** candles |
| **Market profile (TPO)** | TPO POC, value area, Initial Balance + range extension, single prints, poor high/low, day-type (Normal / Trend / Neutral…) | **index** candles |
| **Open interest** | PCR, max pain, call-wall (resistance) / put-wall (support), per-strike buildup vs day-open (writing/unwind) | **ATM ± N option chain** |
| **Imbalance** | synthetic cumulative delta, buy/sell pressure, volume spikes, delta divergence, absorption | **futures** candles |
| **Opportunities** | long/short signals requiring ≥ N agreeing factors, ranked by confluence score | all of the above |
| **Trade plan** | per-signal instrument (futures / buy / sell), entry, structural stop (vol-clamped), laddered targets, R:R | signal + levels |
| **Paper trading** | auto-trades qualified signals, tracks win-rate / avg-R / expectancy so you can see if the edge is real | signals + forward price |

### Why futures + options, not just the index
Nifty and Bank Nifty are **indices** — they have no traded volume or OI of their
own. So the engine deliberately splits inputs: **price-action structure** runs on
the index, **volume & market profile** are built from the **near-month futures**,
and **OI analysis** comes from the **option chain around ATM**. Expiries are
resolved *dynamically* from the live instrument dump (never hardcoded), which
matters because NSE keeps changing weekly-expiry weekdays and Bank Nifty's weekly
options were discontinued.

---

## Three modes
- **live** — stream from Zerodha Kite during market hours (records history as it goes).
- **replay** — play a historical intraday dataset through the same engine.
- **sim** — synthetic replay for testing any time, no feed needed.

## Quick start (test it right now, market closed)

```bash
cd nifty-live-tracker
pip install -r requirements.txt
python run.py --mode sim        # opens http://127.0.0.1:8899
```

`sim` mode replays a **synthetic but realistic** trading day (seeded random walk
with an intraday drift, midday volume lull, and an option chain whose OI shifts as
spot moves), so every panel populates without a live feed or market hours. Tune the
replay under `sim:` in `config.yaml` (`speed` = how many market-seconds pass per
real second).

Smoke test without the browser:
```bash
python run.py --mode sim --once   # warms up, prints one JSON snapshot
```

---

## Going live (Zerodha Kite Connect)

1. **Create a Kite Connect app** at <https://developers.kite.trade/> (₹500/mo).
   Copy the **API key** and **API secret**.
2. `cp .env.example .env` and fill in `KITE_API_KEY` / `KITE_API_SECRET`.
3. **Refresh your access token** (Kite tokens expire daily, ~6 AM IST):
   ```bash
   python -m src.kite_auth
   ```
   It prints a login URL → approve in browser → paste back the `request_token`
   from the redirect. The token is saved to `.kite_token`.
4. **Run live** during market hours (09:15–15:30 IST):
   ```bash
   RUN_MODE=live python run.py
   # or: python run.py --mode live
   ```

Do step 3 once each trading morning (or wire it into a login-flow automation).

### Alerts (optional but recommended)
- **Telegram:** message `@BotFather` → `/newbot` → put the token in
  `TELEGRAM_BOT_TOKEN`. Message your bot once, open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` to get your chat id →
  `TELEGRAM_CHAT_ID`.
- **Desktop:** macOS notifications work out of the box (via `osascript`).

Each signal alerts once per `alert_cooldown_sec` (default 5 min) so a persistent
setup doesn't spam you.

---

## Configuration — `config.yaml`

Everything is tunable without touching code: instruments and strike range,
value-area %, swing sensitivity, ORB window, IB minutes, PCR thresholds, buildup
%, volume-spike multiplier, minimum confluence to raise a signal, dashboard
host/port/refresh, and alert channels. Comments in the file explain each field.

Key knobs:
- `signals.min_confluence` — raise the bar (e.g. 3) for fewer, higher-conviction alerts.
- `instruments[].strikes_each_side` — how wide an option chain to stream for OI.
- `analytics.value_area_pct` — 0.70 is the standard 70% value area.

---

## Timeframes (what to monitor)
Two clocks, on purpose:
- **Execution / price action / volume profile: 5-min** candles — what you watch and act on (`session.candle_minutes`).
- **Market Profile (TPO): 30-min period** by default (`analytics.tpo_period_minutes`) — the classic convention; set `15` for more resolution. Candles stay 5-min; only the TPO letters aggregate. Initial Balance is always the first 60 min.

## Trade plan (decision aid)
Every confluence signal is turned into a concrete, level-based plan shown on the
dashboard: **instrument** (futures for directional days, buy options for
momentum/expiry-aware, sell options into OI walls on rotation days), **entry**,
a **structural stop** clamped to a volatility floor/ceiling (so it's neither a
7-point noise-stop nor a 400-point cliff), **laddered targets** (T1 a measured
move, T2 the far wall), and **R:R**. It's a decision aid — your call to take or skip.

## Paper trading — does the edge hold up?
When a **qualified** signal fires (score ≥ `paper.min_score`, default 3) the tool
opens a paper trade at the current price using the plan's stop and target, then
marks it against forward price: **target hit = win (+R)**, **stop hit = loss
(−1R)**, **session end = flat**. Outcomes are in **R-multiples on the underlying**
(instrument-agnostic — it tests the *signal's* edge, not option pricing). The
dashboard's Paper Trading panel shows win-rate, avg-R, expectancy, gross-R, open
positions, and recent closed trades. Trades persist to `paper/paper_trades.jsonl`
so stats accumulate across days. Run it across many sim/replay days (or live) and
you'll see whether a given confluence threshold actually has positive expectancy —
then raise `paper.min_score` / `signals.min_confluence` until it does.

## Historical replay & live recording

The same analytics + dashboard can be driven by historical intraday data, and the
live feed can record itself into that same format — so live monitoring and
backtest/replay share one pipeline.

### Try replay now (ships with a sample day)
```bash
python tools/make_sample_dataset.py            # writes datasets/sample/
python run.py --mode replay --dataset datasets/sample
```

### Dataset format
A dataset is a folder of per-instrument CSVs (missing files are skipped):
```
datasets/<name>/
  NIFTY_spot.csv       datetime,open,high,low,close              (index)
  NIFTY_fut.csv        datetime,open,high,low,close,volume,oi    (futures)
  NIFTY_options.csv    datetime,strike,leg,oi,ltp                (option chain, optional)
  BANKNIFTY_*.csv      ...
```
The loader tolerates common column-name variants and export formats (TradingView,
Kite, NSE). Any 5-min OHLCV export drops in.

> **Note on daily CSVs:** NSE "full-year" NIFTY 50 CSVs (one row per day) and
> TradingView *strategy reports* are **not** intraday and can't produce a
> meaningful value area or TPO — those need 5-min bars. Use the fetcher below or
> a 5-min chart export.

### Get REAL history from Kite
```bash
python -m src.kite_auth                          # refresh token
python -m src.historical_fetch --date 2025-11-12 # writes datasets/2025-11-12/
python run.py --mode replay --dataset datasets/2025-11-12
```
Fetches real 5-min spot + near-futures (with volume + OI) for that day. (Requires
the historical-data entitlement on your Kite app.)

### Live recording (build real history automatically)
In **live** mode the tracker records the stream to `datasets/live-<date>/` in the
replay format — completed 5-min bars plus periodic full option-chain **OI**
snapshots. So each trading day becomes a real, replayable dataset **including live
option OI** (which historical APIs can't easily provide). Configure under
`record:` in `config.yaml`; set `raw_ticks: true` to also log every tick to
`ticks.jsonl` for maximum granularity. Replay yesterday with:
```bash
python run.py --mode replay --dataset datasets/live-2026-07-20
```

### Granularity
Live streaming uses Kite's **MODE_FULL** — the richest tick available: LTP, OHLC,
last-traded-quantity, cumulative volume, **open interest**, and 5-level market
depth. 5-min candles are built from these ticks; change `session.candle_minutes`
for a finer/coarser view.

## Backtesting the last 3 months (real Kite data)
Reuses your existing `~/Documents/NSE` Kite login — no new API app.

```bash
cd ~/Documents/NSE && python3 kite_login.py          # refresh token (PIN+TOTP)
cd ~/Documents/nifty-live-tracker && source .venv/bin/activate
python tools/fetch_kite_intraday.py --months 3        # pulls 5-min spot + near-fut
python -m src.backtest --datasets datasets/<range> --label real-3mo
open backtests/real-3mo/report.html
```

The backtester runs the full engine + paper logic bar-by-bar across every day
and reports win-rate, avg-R, expectancy, gross-R, max drawdown, profit factor,
and an equity curve. Fetchable history covers **price action, market profile,
volume profile, imbalance, and futures-OI**; per-strike option-chain OI is
live-only (expired option contracts aren't retrievable), so those signals are
validated forward by the live paper engine rather than in the historical run.

## Scheduling & live alerts
See `scheduler/README.md`. In short: the running app sends **Telegram
qualified-setup messages** (with the full plan) during live hours and an **EOD
performance summary** at close; `scheduler/` has a launchd job to auto-start the
tracker each weekday at 09:12 and auto-exit at 15:40, reusing your NSE token.

## The trading-skill bridge (`snapshot.json`)

Every cycle the engine writes `snapshot.json` — the full structured read-out for
both instruments plus ranked signals. That file is the clean hand-off to an LLM
analyst. Two ways to use it:

1. **On-demand:** ask Claude (with the `indian-trading-skills` repo cloned) to
   "read `snapshot.json` and plan a trade" — its **F&O Trade Planner** /
   **Options Strategy Advisor** turn the structural read into strike selection,
   Greeks, and risk-managed structures.
2. **Scheduled:** set up a recurring Cowork task (every 15 min during market
   hours) that reads `snapshot.json`, applies the skill, and messages you a
   plain-English plan. This tracker is the *sensor*; the skill is the *analyst*.

The `indian-trading-skills` collection is analytical, not a live feed — it pairs
naturally as the interpretation layer on top of this engine.

---

## Layout

```
run.py                     entry point (feed → engine → dashboard → alerts)
config.yaml                all tunables
.env.example               secrets template
src/
  config.py                config + secrets loader
  kite_auth.py             daily Kite login / token
  instruments.py           resolve spot + futures + ATM option tokens (dynamic expiry)
  candles.py               tick → 5-min OHLCV aggregation
  state.py                 shared thread-safe market state
  feed.py                  LiveFeed (KiteTicker) + SimFeed
  simbroker.py             synthetic broker + tick generator (sim mode)
  replay.py                ReplayFeed + dataset loader (historical replay)
  historical_fetch.py      Kite 5-min history → replay dataset
  recorder.py              record live stream → replay dataset (bars/OI/raw ticks)
  engine.py                runs all analytics → snapshot dict
  alerts.py                Telegram + desktop + snapshot.json writer
  paper_trade.py           paper-trading validator (win-rate / R / expectancy)
  analytics/
    price_action.py  volume_profile.py  market_profile.py
    oi_analysis.py   imbalance.py       signals.py   trade_plan.py
dashboard/
  server.py                FastAPI + websocket
  static/index.html        single-page live dashboard
```

---

## Notes & limits
- **True order-flow delta** needs full market depth; without it, `imbalance`
  approximates buy/sell pressure from each candle's close-within-range × volume.
  It's a proxy — read it as pressure, not exact delta.
- On a **mid-session live start**, profiles build forward from connect time. For a
  full-day profile from the open, start the app at/just before 09:15, or extend
  `instruments.py` to seed from Kite's historical-candles API (requires the
  historical-data subscription).
- Kite websocket allows up to ~3,000 instruments per connection — the default
  ATM ±12 strikes for two indices is far under that.
- Runs on your own machine (your Mac mini is ideal): a live streamer needs to run
  continuously through the session, which an ephemeral cloud session can't do.

## Daily workflow
1. Morning: `python -m src.kite_auth` (refresh token).
2. ~09:14 IST: `RUN_MODE=live python run.py`.
3. Watch the dashboard; act on Telegram/desktop alerts.
4. Optional: let a scheduled Cowork task read `snapshot.json` + the F&O skill and
   message you trade plans.
