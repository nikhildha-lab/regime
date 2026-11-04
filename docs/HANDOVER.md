# Market Pulse Dashboard · Production Handover

Nov 4, 2026 · @Nikhil

## Summary

Market Pulse is a single-user market dashboard that runs today on one Mac (Nikhil's) at `http://127.0.0.1:8050`. It is not yet production software. It shows regimes, order flow, levels, news and paper-trade records for Indian indices, Nifty 50 stocks, US indices, 9 crypto coins, metals and energy. It places **no orders**: everything is indications and paper trades, and Nikhil decides.

This handover covers what the team needs to run, harden and host it:

- the Market Pulse app and its background loops;
- the three live engines it depends on (Nifty tracker, Dhan order-flow engine, metals tracker);
- the scheduled Claude tasks that write desk notes, news digests and the weekly narrative;
- the gaps to close before production (section 14).

**Out of scope:** the AI Investor platform (Stock Twin, Crypto Twin, Strategy Lab, Sector & Macro) and MASTER\_BACKTESTER. They are separate projects; this doc only notes where they touch Market Pulse.

| Item | Today |
| --- | --- |
| Host | One macOS machine, user `nikhildhawan`, repo `~/Documents/synaptic-main/Backtrader_App` |
| Runtime | Python 3.9 (Mac Command Line Tools) in `.venv`, stdlib HTTP server, no framework |
| Front end | One HTML file, `dashboard/market_pulse_template.html` (\~270 KB, vanilla JS, no build step) |
| Storage | Files only (CSV, JSON, JSONL) under `data_store/` (1.4 GB) and `~/Documents/dhan/orderflow/data` (130 MB) |
| Access | Bound to `127.0.0.1`, no login, no TLS |
| Users | 1 |

## Architecture

Four processes on one Mac pull from five external sources and share state through files in `data_store/`. The browser only talks to Market Pulse.

&#91;embedded content: architecture · 5 sources, 4 processes, 1 file store\]

| Process | How it starts | Port | Hours |
| --- | --- | --- | --- |
| Market Pulse (`app/server.py`) | `run_market_pulse.sh`, from the supervisor | 8050 (page + API), 8051 (intraday API), 8052 (live stream, SSE) | Always |
| Nifty tracker (`~/Documents/nifty-live-tracker/run.py`) | `start-live.sh`, from the supervisor | 8899 | Weekdays 09:00 to 15:35 IST |
| Dhan order-flow engine (`~/Documents/dhan/orderflow/of_engine.py`) | Started with the Nifty tracker | none (writes files) | Stops itself at 15:31 IST |
| Metals tracker (`~/Documents/metals-tracker/run_forever.sh`) | Auto-started by Market Pulse when it is down | 8897 | Always (24/7 markets) |
| Dhan token keep-alive (`dhan_keepalive.sh`) | From the supervisor | none | Always |

The supervisor is `~/Desktop/Start Trading Desk.command`: a bash loop in a Terminal window that checks every 60 s and starts whatever is down. It must stay open. The crypto engine runs inside the Market Pulse process (since 1 Nov 2026).

## Code and folder layout

All Market Pulse code lives in `~/Documents/synaptic-main/Backtrader_App`. That repo also holds an older Backtrader trading stack (Docker files, `frontend/`, `run_api.py`, many one-off scripts) which Market Pulse does **not** use. Ship only the paths below.

| Path | What it is |
| --- | --- |
| `app/server.py` | Entry point. HTTP server on 127.0.0.1:8050, the 5-minute Worker, starts the background loops |
| `app/updater.py` | Data downloads (Yahoo, Dhan, NSE end of day), starts the stream, flow, option-chain and news threads |
| `app/livedesk.py` | Live desk: every `/api/livedesk/*` route, the 2-second poll loop, footprint views, tracker auto-start |
| `app/stream.py` | Live stream hub on :8052 (Server-Sent Events, 1 snapshot a second): Binance, Yahoo poll + Yahoo websocket, Hyperliquid, Dhan |
| `app/news.py`, `app/tabnews.py`, `app/narrative.py` | Headlines, shock watcher, news sentiment; per-tab swing catalysts; weekly global narrative facts |
| `app/hourverdict.py`, `app/levelmap.py`, `app/prep.py`, `app/reflevels.py`, `app/stockview.py` | Verdict, blocks, overnight prep and reference levels (VAH / VAL / POC / VWAP / IB / PDH ...) |
| `app/break4h.py`, `app/touchtrades.py`, `app/domcards.py`, `app/xsecpaper.py`, `app/papertrades.py` | The four paper-trade rules and the combined paper-trade list |
| `app/crypto_live.py`, `app/cryptoengine/` | In-process crypto engine (Binance futures, 9 coins) |
| `app/health.py`, `app/edgeboard.py`, `app/desk_facts.py` | System health checks, Edge board, no-AI desk facts for the scheduled tasks |
| `app/dhan_live.py`, `app/india.py`, `app/optionchain.py`, `app/oiwatch.py`, `app/crossmkt.py`, `app/extras.py`, `app/flow.py` | Market-data helpers |
| `scripts/export_dashboard_data.py` (+ `india_desk.py`, `global_desk.py`) | Rebuilds `market_pulse_live.json` every cycle; runs as a subprocess |
| `src/common/master_brain/markets.py`, `src/common/regime_engine/` | Regime engine: market list, bars per timeframe, regime and scores |
| `scripts/research/` | Offline studies; `strategy_regime_3y.py --matrix` builds the strategy x regime matrix |
| `scripts/agent/` | Helpers the scheduled Claude tasks call (`agent_log.py`, `guard_check.py`, `narrative_check.py`, ...) |
| `dashboard/market_pulse_template.html` | The whole front end |
| `app/requirements.txt` | Python packages (pandas, numpy, scipy, scikit-learn, hmmlearn, curl\_cffi, certifi, protobuf, websocket-client) |

Sibling folders the app reads or starts: `~/Documents/nifty-live-tracker` (tracker, `.dhan_token`, keep-alive), `~/Documents/dhan/orderflow` (order-flow engine), `~/Documents/metals-tracker` (metals tracker).

Most modules hot-reload when their file changes (`livedesk._hot`). The stream, crypto engine, updater threads, `levelmap` and `server.py` itself need a restart.

## Data sources and external connections

Every source is free or uses Nikhil's own broker login. None of them has a service-level agreement, so production must treat each one as "may fail at any time".

| Source | Used for | How | Auth | Notes |
| --- | --- | --- | --- | --- |
| Yahoo Finance | Daily and intraday bars for indices, stocks, FX, rates, commodities; live quotes | HTTP via `curl_cffi` (browser fingerprint) + Yahoo websocket (protobuf) in `stream.py` | None | Unofficial. Rate limits and format changes happen. The 5-minute Yahoo pass is the slowest step |
| Dhan (broker) | Nifty / Bank Nifty futures and options ticks, option chain, order flow | Dhan websocket in the tracker and order-flow engine; REST for option chain | Daily access token in `nifty-live-tracker/.dhan_token` | Token lasts about 24 h. `dhan_keepalive.sh` renews it; a fresh login is needed when it lapses |
| Binance USD-M futures | Crypto bars, live trades, depth for order flow and DOM | REST `fapi.binance.com` + websockets | None (public) | Some regions block Binance; check before moving hosts |
| Hyperliquid | Live perps for S&P 500, Nasdaq, Nikkei, Kospi, gold, silver, crude, Brent, Nifty; metals tracker; reference levels | `api.hyperliquid.xyz/info` REST + websocket (`xyz` dex) | None (public) | Prices are perp prices, not cash index prints. They track closely but can drift |
| NSE | End-of-day bhavcopy, FII / DII data, corporate announcements | HTTP | None | NSE blocks bots aggressively; retries and a browser-like client are needed |
| News feeds (RSS) | Headlines, shock watcher, sentiment, tab catalysts | RSS over HTTP in `news.py` / `tabnews.py` | None | CoinDesk and Moneycontrol return 403 today |
| Claude (scheduled tasks) | Daily narrative, desk notes, learner, health check | Claude cloud tasks that reach the Mac through the desktop app bridge | Nikhil's Claude account | See Scheduled AI tasks |

No source here is licensed for redistribution. If the dashboard is shown to anyone beyond Nikhil, the team must check each provider's terms first, and should expect to replace Yahoo and NSE scraping with a paid feed.

## Dashboard tabs and sections

One page, `dashboard/market_pulse_template.html`, served at `/`. Data comes from `market_pulse_live.json` (rebuilt every 5 min), the live stream on :8052 (1 snapshot a second) and the `/api/livedesk/*` routes (polled every 2–60 s).

| Tab | Sections, top to bottom |
| --- | --- |
| Overview | Global Indices strip (Nifty, Sensex, S&P 500, Nasdaq, Dow, Nikkei, Kospi, Shanghai, Bitcoin, gold, crude; live, 1 s) · Global Narrative · This Week · All Markets table (Latest Price, Regime band, Impact scores) · News & Events with News Sentiment panel · Regime History vs Nifty · Last 90 Sessions |
| Indian Market | Now strip · Now · Verdict · block line chart with dashed levels · Order Flow (last 12 candles, any timeframe) · Levels & Blocks · Nifty · option cards · paper trades · India catalysts news · Nifty 50 stock view |
| US Market | Now strip · verdict · block chart · levels · US catalysts news |
| Crypto | Now strip · verdict · block chart with Binance-based levels · order flow / DOM · crypto engine · crypto catalysts news |
| Metals | Now strip · verdict · block chart with Hyperliquid levels · metals tracker · metals catalysts news |
| Commodities | Now strip · verdict · block chart · commodities catalysts news |
| Intraday / Live Desk | Live desk view, footprint, DOM cards, touch-minute cards, 4H block break, Edge board, combined paper trades |

**Dashed levels on block charts:** VAH / VAL / POC (purple, 5-day rolling volume profile), VWAP (amber, last session), IB high / low (blue, first hour), PDH / PDL, PWH / PWL, PMH / PML (grey). VA, POC, VWAP and IB show within 4 % of price; the rest within 2 %.

**Labels:** the page states "paper / indications only" and never shows an order button. Keep that in production.

## Backend APIs

All routes are plain GET returning JSON, with no authentication. They are safe today only because the server binds to 127.0.0.1.

**Server (`app/server.py`, port 8050)**

| Route | Returns |
| --- | --- |
| `/` | The dashboard page (template + latest JSON) |
| `/api/pulse`, `/api/status`, `/api/refresh` | Latest dashboard JSON, worker status, force a refresh cycle |
| `/api/live`, `/api/flow`, `/api/intraday` | Live quotes, cross-market flow, intraday bars (intraday also on :8051) |
| `/api/stream` | Proxy to the Server-Sent Events hub on :8052 |
| `/api/desk_facts`, `/api/agent`, `/agent/setup` | No-AI desk facts and agent outputs for the scheduled tasks |
| `/api/backtester/_snapshot*` | Old backtester snapshot (legacy) |

**Live desk (`app/livedesk.py`, under `/api/livedesk/`)**

| Route | Returns |
| --- | --- |
| `view`, `diag` | Live desk state; diagnostics |
| `hourverdict`, `prep`, `levelmap`, `oiblocks`, `reflevels` | Verdicts, overnight prep, blocks and reference levels |
| `break4h`, `domcards`, `dom`, `blockwatch`, `papertrades`, `edgeboard`, `stratmatrix` | Paper-trade rules, DOM, Edge board, strategy x regime matrix |
| `n50`, `stock`, `stockpulse` | Nifty 50 stock views |
| `narrative`, `tabnews` | Global narrative facts + story; per-tab catalyst news |
| `start_tracker` | Start / restart the Nifty, crypto or metals tracker |
| `run_tool?name=` | Admin actions: `restart_pulse`, `news_now`, `tabnews_now` and others |

**Production must:** put `start_tracker` and `run_tool` behind authentication (they start processes), and put every route behind auth before binding to anything other than localhost.

## Scheduled AI tasks

These are Claude scheduled tasks on Nikhil's Claude account. Each run is a fresh Claude session that reaches the Mac through the Claude desktop app, reads desk facts, and writes JSON into `data_store/agent/out/`. If the Mac is asleep or the app is closed, the run fails quietly. They write notes only; they never place orders and never change rules.

| Task | When (IST) | Writes |
| --- | --- | --- |
| Global narrative · daily | 07:17 daily | `global_narrative.json` (story shown when under 36 h old) |
| News Watcher | 08:05 weekdays | `news.json` notes |
| Crypto C1 | 07:05 daily | `crypto.json` |
| Desk Lead L1 / L3 | 08:45, 16:45 weekdays | `intraday.json`, `swing.json` |
| US Desk US1 | 18:40 | `us.json` |
| Strategy brain (Porter) | Tue / Thu / Sat 00:10 | research notes |
| Desk Learner | Sat 19:10 | `learning_weekly.json` |
| Synaptic weekly health check | Sun 18:46 | health report |
| AI Investor x5 (Stock Twin, Strategy Lab, Crypto Twin, Sector & Macro, Portfolio Twin) | various | **Separate product. Do not touch.** |

Paused, kept for reference: Desk Lead hourly L2, specialists pre-market / opening / close, US2, US3, Crypto C2 / C3 / C4, Governor.

**For production:** the dashboard works without these tasks; only the narrative story and agent notes go stale. Moving them off the Mac means either running them from a server with the Claude API, or keeping them on Nikhil's account and accepting the Mac dependency.

## Run book

**Start everything (today, on the Mac)**

1. Double-click `~/Desktop/Start Trading Desk.command`. It keeps the Mac awake and checks every 60 s.
2. It starts Market Pulse if port 8050 is down, the Dhan keep-alive if a token exists, and the Nifty tracker + order-flow engine on weekdays 09:00–15:25 IST.
3. Open http://127.0.0.1:8050.

**Start Market Pulse by hand**

```
cd ~/Documents/synaptic-main/Backtrader_App
./run_market_pulse.sh --no-browser      # = .venv/bin/python app/server.py --port 8050 --every 5
```

**Daily Dhan login (when the token lapses)**

Run `~/Desktop/Dhan-Login.command`. The owner pastes client id and token. Nobody else should handle these values.

**Common actions**

| Need | Do |
| --- | --- |
| Restart Market Pulse | `curl 127.0.0.1:8050/api/livedesk/run_tool?name=restart_pulse` (supervisor brings it back within \~1 min) or `Restart Market Pulse.command` |
| Restart a tracker | `Restart Nifty Tracker.command`, `Restart Crypto Tracker.command`, or `/api/livedesk/start_tracker?name=...` |
| Refresh news now | `run_tool?name=news_now`, `run_tool?name=tabnews_now` |
| See what is wrong | `/api/livedesk/diag`, `/api/status`, `logs/market_pulse.log` |

**When something looks wrong**

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Page loads, numbers old | Worker stuck on Yahoo | Check `/api/status`; restart Market Pulse |
| Nifty order flow empty in market hours | Dhan token lapsed or tracker down | Re-login; restart Nifty tracker |
| Global strip frozen | Stream hub (:8052) died | Restart Market Pulse (stream needs a restart, not hot reload) |
| News empty | Feed down or `news_seen.json` damaged | Check log; the file now loads safely and is rebuilt |
| Narrative says stale | 07:17 task did not run (Mac asleep) | Run the task by hand or wait for the next day |
| Code change not showing | Module needs restart | Restart Market Pulse |

## Configuration and secrets

**Configuration** is spread across code and files. There is no single config file.

| Where | What |
| --- | --- |
| `app/server.py` flags | `--port 8050`, `--every 5`, `--no-browser` |
| Ports in code | 8050 dashboard, 8051 intraday API, 8052 stream, 8897 metals tracker, 8899 Nifty tracker |
| `nifty-live-tracker/config.yaml` | Broker (dhan), instruments, session times, footprint, paper, alerts, recording |
| `metals-tracker/config.yaml` | Port, Hyperliquid URLs |
| `src/common/master_brain/markets.py` | Market list, symbols, availability times |
| `stream.py`, `updater.py` | Symbol lists (`YAHOO`, `HL_COINS`, `YAHOO_SEED`) |

**Secrets** (never print, never commit, never paste into chat or tickets):

| Secret | Where today | Production home |
| --- | --- | --- |
| Dhan access token + client id | `~/Documents/nifty-live-tracker/.dhan_token` (plain file) | Secrets manager, mounted read-only to the tracker only |
| Older broker tokens | `Backtrader_App/access_tokens/` | Review, then revoke and remove |
| Optional API keys (health shows them as "ok (optional)") | Environment / local files | Secrets manager |

Before the repo leaves the Mac: scan git history for tokens, rotate anything that was ever committed, and add `.dhan_token`, `access_tokens/` and `data_store/` to `.gitignore`.

## Data stores, retention, disk

There is no database. Everything is CSV, JSON and JSONL files under `Backtrader_App/data_store/` (about 1.4 GB) plus the Dhan order-flow folder (about 130 MB). The Documents volume has about 110 GB free, so disk is not urgent.

| Folder | Size | Holds | Retention today |
| --- | --- | --- | --- |
| `research/` | 642 MB | Backtest and study outputs | Kept forever |
| `market_data/` | 357 MB | Daily and intraday bars (incl. `yahoo_daily_deep.csv`) | Grows; rebuilt from Yahoo |
| `flow/` | 219 MB | Binance 1m, option chain, OC live, GIFT Nifty, Upstox ticks, stream status | Grows |
| `livedesk/` | 59 MB | Verdicts, blocks, DOM cards, touches, paper-trade records, footprint, marks | Archive folder; mostly kept |
| `snapshots/` | 37 MB | Dashboard snapshots | Kept |
| `agent/`, `agent/out/` | small | News, sentiment history, 7-day headlines, tab news, Claude task outputs | Rolling (news 48 h, headlines 7 days) |
| `dashboard/market_pulse_live.json` | small | The page's main data, rebuilt every 5 min | Overwritten |
| `~/Documents/dhan/orderflow/data/of/<day>/` | 130 MB | Per-second bars, footprint JSONL, `live.json` | One folder a day, kept |

**Write safety:** key JSON files are written atomically (temp file + rename). Several loops write files in parallel, so any new writer must do the same. `news_seen.json` broke once from concurrent writes.

**Production should:** move paper-trade records and verdict history into a real database (SQLite at minimum, Postgres if multi-user), add a retention job for `flow/` and order-flow days, and back up `livedesk/` (the paper-trade record is the only data that cannot be re-downloaded).

## Monitoring and health checks

**What exists**

| Check | Where | What it tells you |
| --- | --- | --- |
| Health module | `app/health.py`, surfaced on the dashboard | Each feed and file: fresh, stale or failing. Missing optional API keys read "ok (optional)" |
| Diagnostics | `/api/livedesk/diag` | Live desk loops, tracker ports, last update times |
| Worker status | `/api/status` | Last cycle time, errors |
| Stream status | `data_store/flow/stream_status.json` | Per-source last tick for the 1 s stream |
| Stale badges | On the page | "Last session" or age stamps when data is old |
| Logs | `logs/market_pulse.log`, tracker logs | Errors and restarts |
| Weekly health check | Claude task, Sun 18:46 | Written review of the week |
| Supervisor | `Start Trading Desk.command` | Restarts dead processes every 60 s |

**What is missing for production**

- No alerting. Nothing pages anyone when a feed dies; someone must look at the page.
- No metrics store, so no history of uptime or latency.
- Logs are plain text with no rotation.
- No external uptime check (the server is localhost only).

Suggested minimum: a `/healthz` route that returns non-200 when any core feed is stale beyond its limit, an uptime monitor on it, log rotation, and an alert on Dhan token expiry the evening before.

## Research verdicts and guardrails

The team must keep these findings attached to the screens. They decide what the dashboard is allowed to claim.

| Idea | Verdict | What the dashboard does with it |
| --- | --- | --- |
| Order flow as a signal | No edge alone. As a filter, about +0.10R | Shown as context; never a stand-alone signal |
| Costs | Costs decide most results | Every edge is judged gross first, then net |
| Regime | Useful for sizing, not direction. The ±10 direction scores failed testing | Regime band shown; direction scores still need demoting (open) |
| Strong blocks | Held 77–80 % of the time | Used for stops and targets, not entries |
| Fading rejections | Lost money after costs | Not offered |
| VIX sizing | No edge | Not used. Crypto implied-vol sizing beat luck and is kept as a study |
| DOM paper test, day 1 | 84 trades, +0.04R gross, −0.14R net | Running as paper only |
| Hourly verdict record | 57 % hit, +5.4 bps (below costs) | Shown with its record, labelled context |
| `odte_ladder` strategy | Profit factor 17 — too good to trust, not yet checked | Treat as suspect until checked |

**Paper rules running:** 4H block break, touch-minute option cards, cross-section momentum basket, DOM hold / break.

**Hard guardrails (do not remove in production):**

- Paper and indications only. No order routing, no broker order API calls, no "buy" / "sell" buttons.
- Forward paper results never change rules automatically. A person reviews and decides.
- Every number on screen comes from data or a recorded test. Nothing is invented or filled in.
- Not financial advice; the owner makes every decision.

## Known issues and limitations

| Issue | Impact | Notes |
| --- | --- | --- |
| `odte_ladder` profit factor 17 unchecked | Could mislead | Likely a lookahead or cost bug; check before showing |
| ±10 direction scores still on screen | Implies direction skill that failed tests | Demote to sizing / context |
| Cross-section basket stuck open | Paper record wrong | Close-out logic needs a fix |
| Live desk poll stalls \~2 min during the Yahoo updater pass | Brief freezes | Move Yahoo pass off the shared thread |
| CoinDesk and Moneycontrol feeds return 403 | Less news | Replace or drop |
| Kospi and Shanghai over Yahoo websocket not verified | May fall back to slower poll | Check during Asian hours |
| Aluminium has no Hyperliquid levels | No dashed levels on that chart | No perp source |
| Sensex has no order flow or blocks | Index view only | Dhan feed covers Nifty / Bank Nifty only |
| Global index tiles use Hyperliquid perps | Small drift from cash index | Labelled as perp prices |
| Liquidation zones | Not shown | Real position data is not freely available; estimates were rejected |
| Some modules need a restart, others hot-reload | Confusing deploys | Remove hot reload in production |
| Device clock (VM) is UTC, desk is IST | Time bugs | Always use explicit time zones |

## Production readiness gaps and recommended steps

| # | Gap today | Risk | Recommended step |
| --- | --- | --- | --- |
| 1 | Runs on one Mac, kept awake by `caffeinate` | Sleep, reboot or travel stops everything | Move to a small Linux server or VM in an India region (low latency to Dhan / NSE) |
| 2 | No auth, no TLS; admin routes start processes | Anyone who can reach the port controls it | Reverse proxy (nginx / Caddy) with TLS and login; keep app bound to localhost behind it |
| 3 | Secrets in plain files | Token leak | Secrets manager; rotate the Dhan token and anything in `access_tokens/` |
| 4 | Daily Dhan token needs a manual login | Order flow goes dark when it lapses | Keep the keep-alive; add an expiry alert; document the owner-only login step |
| 5 | Files, not a database | Concurrent-write damage, no history queries | SQLite or Postgres for paper trades, verdicts, news history |
| 6 | Stdlib `ThreadingHTTPServer` | No timeouts or limits | Move routes to FastAPI / Starlette under uvicorn, or at least put a proxy in front |
| 7 | Supervisor is a bash loop | No restart backoff or logs | systemd units (or Docker Compose) per process: pulse, stream, trackers, order-flow engine |
| 8 | Hot reload in the live process | Half-loaded code | Turn off hot reload; deploy by restart |
| 9 | No tests, no CI | Regressions reach the screen | Smoke tests for each route, a JSON schema check on `market_pulse_live.json`, CI on every push |
| 10 | Python 3.9 (Mac system) | Old runtime, mixed venvs | Python 3.11+, pinned `requirements.txt` with exact versions, one venv per service |
| 11 | Repo clutter (old Backtrader stack, CSVs, token folder) | Confusion, leaks | New clean repo with only the paths in Code and folder layout |
| 12 | Free / unofficial feeds | Breakage, terms of use | Paid feed for India and US data if shown to others; keep a fallback chain |
| 13 | Claude tasks depend on the Mac | Narrative goes stale | Run from the server via the Claude API, or accept staleness |
| 14 | Plain-text logs, no alerts | Silent failures | Log rotation, `/healthz`, uptime alerts |
| 15 | Mixed UTC / IST handling | Time bugs | Store UTC, show IST, set server TZ explicitly |

**Suggested order:** 3 → 11 → 1 + 7 → 2 → 9 → 5 → 14 → the rest. Keep the guardrails in Research verdicts and guardrails as acceptance criteria for every release.

## References: project docs and change log

The "Platform engine" project on claude.ai holds the working notes behind this dashboard. Most useful for the team:

| Doc | Covers |
| --- | --- |
| `claude/deep-audit-2026-11-03.md` | Latest full audit |
| `claude/ui-trust-fixes-2026-11-03.md` | UI trust fixes and Now strip |
| `claude/dropped-strategies-2026-11-03.md` | What was removed and why |
| `claude/audit-data-and-connections-2026-11-01.md` | Data sources and connections |
| `claude/maintenance-audit-2026-10-31.md` | Maintenance audit |
| `claude/global-narrative.md`, `claude/tab-news-catalysts.md` | Narrative and per-tab news |
| `claude/sensex-added.md` | Sensex as third Indian index |
| `claude/strategy-by-regime-3y.md`, `claude/vix-sizing-no-edge.md`, `claude/real-orderflow-test.md` | Research verdicts |
| `claude/dom-strategy-test-plan.md`, `claude/paper-trades-one-place.md` | Paper rules |

**Recent changes (early November 2026)**

- Block Story removed fully; modules moved to `_archive/2026-11-03/retired_modules/`.
- Scheduled tasks trimmed; AI Investor jobs untouched.
- Now strip on every tab; Deeper Views and the old strategy catalogue removed.
- Block charts: dashed VAH / VAL / POC / VWAP / IB / PD / PW / PM levels on every market; chart moved below Now · Verdict.
- Overview: Global Indices strip (live, 1 s), Global Narrative · This Week, All Markets redesign, News Sentiment panel.
- Per-tab swing-catalyst news for India, US, Crypto, Metals, Commodities.
- Order Flow shows the last 12 candles on every timeframe.
- Sensex added as the third Indian index.
- Liquidation zones tried and dropped (no real position data).

Backups of every edited file are in `_archive/2026-11-03/backups/`.

## Design system

The front end is one file, `dashboard/market_pulse_template.html` (plain HTML + CSS + vanilla JS, no build step). Full detail is in `docs/DESIGN.md` in the repo.

**Colour tokens** (always use tokens, both themes required)

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--bg` / `--surface` / `--surface-2` | #EEF2F5 / #FFFFFF / #F6F8FA | #0A1016 / #111A22 / #16212B | Page, cards, chips |
| `--ink` / `--muted` / `--faint` / `--line` | #13212E / #5B6979 / #8C98A5 / #DAE1E7 | #E3E9EF / #97A5B3 / #6E7C8A / #22303C | Text and borders |
| `--accent` | #0E6C73 | #43B4B4 | Selected chip, focus ring |
| `--bull` | #1E8A4F | #3CC27C | Up, support |
| `--bear` | #C23A31 | #EF625A | Down, resistance |
| `--neutral` | #7D8894 | #8F9AA6 | Flat, mixed |
| `--wait` | #C77D05 | #E9A23B | Wait verdicts, price inside a block, stale |
| `--hero` → `--hero-2` | #0D1923 → #13232F | #081118 → #0F1C26 | Dark hero band, 160° gradient |

Chart levels: VAH / VAL / POC purple #8B7CF6, VWAP amber #E8A33C, IB blue #2F9BD6, PD / PW / PM grey. Blocks: strong 22 px band at 0.85 opacity, medium 14 px at 0.6, weak 7 px at 0.4.

**Type:** Archivo 500–800 for headings (h1 30 px / 800, h2 17 px / 700), IBM Plex Sans 14 px for body, IBM Plex Mono with tabular numerals for every number. Labels in Title Case, plain short words.

**Layout:** max width 1480 px, 20 px gutters, 18 px vertical gap; cards 10 px radius, 1 px border, 16 px padding; hero 14 px radius, two columns collapsing under 1000 px; breakpoints 1250 → 520 px, everything stacks under 640 px. Pills fully rounded 12 px / 600; chips 8 px radius with accent fill when pressed; 2 px accent focus ring.

**Each market tab, top to bottom:** Now strip → Now · Verdict → block line chart → Order Flow → Levels & Blocks → paper-trade cards → tab catalyst news.

**Refresh:** live prices 1 s (SSE), live desk 2–15 s, verdicts and levels 30–60 s, main JSON 5 min, status 10 s, tab news 10 min, narrative facts 30 min and story daily.

**UI rules:** no order buttons; "—" when data is missing, never a guess; one fact shows one number across the page; test both themes at 375 px.

## Strategy and engine logic

All paper or context only. Exact thresholds for every rule are in `docs/STRATEGIES.md`; the code file named in each row is the source of truth.

**Regime engines**

| Engine | Logic | Used for |
| --- | --- | --- |
| Exposure regime (`exposure_regime.py`) | First match: High Volatility (vol pct > 80, or IV pct > 90, or vol pct > 60 with 60-day drop > 10 %) → Bull / Bear Trend (ER20 > 0.30, ADX14 > 22.5, 20-day return sign, close vs EMA200) → Compression (vol pct < 30, ER20 < 0.15) → Range | Size: Bull 1.0, Compression 0.9, Range 0.6, High Vol 0.2, Bear 0.15 |
| Per-timeframe regime (`tf_regime.py`) | Same five names on each timeframe's own bars; trend score ±3 instead of 20-day return | Understanding only |
| Score v2 (`score_v2*.py`) | Direction −100…+100 = mean of 2·Φ(z)−1 feature scores vs own 1-year history; Trend and Vol 0–100; label held K bars | Context; ±10 direction scores failed tests |
| HMM challenger (`hmm_regime.py`) | 3-state HMM on return + vol, yearly refit, forward filter | Not live |

**Hourly verdict** (`hourverdict.py`): avg of 5m and 15m scores; up if both "Up" and avg > −1 or avg ≥ 3, down mirror, else mixed; strong if |avg| ≥ 5. Inside a medium / strong block → *At a block – wait*. Down near strong support → *Into strong support – wait*; down near resistance → *Sell on rise*; else *Trend continuation ↓* (strong) or *Sell on rise*. Up is the mirror. Mixed → *Sideways – wait*.

**Blocks** (`levelmap.py`): last 10 sessions in spot. Weights: week / month high-low 3 (★), prev day high-low 2, close 1, 10-day POC 2.5, VAH / VAL 1.5, 5-day POC 2, high-volume nodes 1.5, 30-min swings 1.0→0.5, option strikes (OI share ×6 + fresh writing ×2 + volume ×2). Score within ± band (Nifty 15 pts, Bank Nifty 40); peaks 4 bands apart; edges at 80 % of peak. Use for stops and targets.

**Reference levels** (`reflevels.py`, `levelmap.py`): PDH / PDL, PWH / PWL, PMH / PML, 5-day VAH / VAL / POC (70 % value area), IB (India 09:15–10:15; 24/7 markets 00:00–01:00 UTC), VWAP.

**Setups + guard** (`setups.py`, `prep.py`): six votes (trend, impact, options OI, futures OI, PCR change, order-flow delta); ≥ +3 Buy Dips, ≤ −3 Sell Rises; needs R:R ≥ 1.2. Guard: stand aside on budget / election-result day; no entries 09:55–10:35 on RBI day; half size and no entry before 10:15 after a gap > 0.6 × average range; half size when 15-min is Volatile; nothing new in shock mode.

**Paper rules running**

| Rule | Entry | Exit | Costs |
| --- | --- | --- | --- |
| 4H block break (BTC, ETH, SOL, Gold) | 4H bar enters a 30-day block and closes beyond the far edge; crypto needs net taker volume ≥ 5 % in the break direction | Stop past near edge +0.04 %; out after 12 bars | 0.14 % round trip |
| Touch-minute cards (Nifty, Bank Nifty) | Key-level touch minute with delta / volume > +0.2 at support or < −0.2 at resistance; enter bounce at that close | 30 min later at market, no stop | Nifty \~18 pts, Bank Nifty \~45 pts |
| DOM hold / break (index futures + 48 stocks) | HOLD: comes into zone, holds, leaves same side within 2 min with book leaning ≥ 0.10 and absorption. BREAK: beyond far side 30 s, book ≥ 0.15, delta ≥ 15 % (stocks: defending qty −30 %) | Target next level ≥ 2R; 60 min or 15:20; max 4 a day per market | Nifty 6 pts, Bank Nifty 14 pts, stocks 0.08 % |
| Momentum basket (29 crypto perps) | 00:00 UTC: long top 5, short bottom 5 by 72 h return | Hold 24 h | 0.10 % per rebuild |

**Crypto engine** (`app/cryptoengine/`): stance Buy dips / Sell rallies / Fade edges / Stand aside; reversal setups graded A+…C from level tier, absorption, divergence, regime and change of character; paper book opens at a minimum score with a structural stop and target (+R or −1R).

**News** (`news.py`, `tabnews.py`): severity 3 / 2 / 1 by keyword tiers; shock mode for 3 h on a 2-source severity-3 headline or a 1 % / 15 min (2 % day) index move or VIX +10 %. Sentiment = 5 × weighted mean tone (weight = severity × sources ≤ 3 × half-life 12 h), −10…+10; Positive ≥ 2, Negative ≤ −2. Tab catalysts keep only items naming a tab asset with a known catalyst; max 2 per name, 10 per tab, 48 h.

**Global narrative** (`narrative.py`): computed facts (1d / 1w / 1m change, big-week flag at 2× weekly std, 50 / 200-day crosses, 52-week highs / lows, 10 % / 20 % off high) + the daily AI story.

## Scheduled task prompts

The seven Market Pulse tasks, prompts copied exactly as they run today (also in `docs/SCHEDULED_TASKS.md`). They read persona and activity files from `data_store/agent/personas/` and desk memory from `data_store/agent/memory/`; both are in the repo. On a new machine, change the folder path and replace "device shell on Nikhil's Mac" with the new runner's shell. AI Investor tasks and the MASTER\_BACKTESTER Porter are separate products and are not included.

### 1. Global narrative · daily — `17 7 * * *` IST

```text
You write the "Global Narrative · This Week" story for Nikhil's Market Pulse dashboard (Overview tab). It explains, in simple language, what drove global equity indices over the last 7 days: geopolitical changes, macro indicators and turning points. Research and context only: no trade advice, no "buy" / "sell" wording, never place orders. Never invent numbers: every number comes from the facts file or a page you fetched.

Use the device shell (mcp__remote-devices__device_bash) on Nikhil's Mac. Platform folder: $HOME/mnt/Documents/synaptic-main/Backtrader_App. Run python from that folder. The shell cannot reach news sites: use WebSearch (mode "extended") for news. Short python3 snippets only; never print whole files. Never print secrets.

1. Facts (already computed, no AI): run
   python3 -c "import sys,json; sys.path.insert(0,'app'); import narrative as N; print(json.dumps(N.facts(), default=str)[:15000])"
   It gives: indices (Nifty 50, S&P 500, Nasdaq 100, Dow, Euro Stoxx 50, Nikkei 225, Kospi, Hang Seng, Shanghai Composite) with 1-day / 1-week / 1-month change, distance from the 52-week high and mechanical turning points; cross-asset 1-week moves (gold, Brent, dollar index, US 10Y, US VIX, Bitcoin); macro readings at a 1-year extreme; and the week's biggest headlines from the dashboard's news watcher.
2. News: search the last 7 days for (a) geopolitical events that moved markets (wars, sanctions, tariffs, elections, central-bank surprises, oil supply), (b) the week's macro data and central-bank moves (Fed, ECB, BoJ, PBoC, RBI, inflation, jobs, PMIs), (c) the reasons behind the biggest index moves in the facts (especially any flagged turning point). Prefer Reuters, Bloomberg, FT, WSJ, CNBC, Nikkei Asia, Economic Times, Mint. Paraphrase; no quote over 10 words.
3. Write data_store/agent/out/global_narrative.json (create the folder if needed) with exactly these fields:
   {"as_of": "YYYY-MM-DD HH:MM" (IST now), "week_of": "DD Mon - DD Mon" (the last 7 days),
    "headline": one sentence, the week's main story,
    "summary": [2-3 short paragraphs: what happened, why indices moved, what changed vs last week],
    "themes": [3-5 objects {"name": e.g. "Iran war and oil", "what": 1-2 sentences, "impact": which indices / assets it moved and how, with numbers from the facts}],
    "turning_points": [objects {"market": index name, "what": the turning point from the facts plus the likely reason}] - only those in the facts or clearly reported,
    "watch": [3-5 objects {"when": date or "this week", "what": scheduled event or risk to watch next}],
    "sources": [4-8 objects {"title", "url"}]}
   Write it with a short python snippet (json.dump, ensure_ascii=False) to a temp file and os.replace it into place.
4. Check: python3 scripts/agent/narrative_check.py. If it prints BLOCK, fix the file and run it again (once). Change no other file.

Finish with one line: the headline.
```

### 2. News Watcher — `5 8 * * 1-5` IST

```text
You are the News Watcher for Nikhil's Synaptic trading desk (Indian market). News is rare but critical: on quiet runs say "no big news" in one line; on big days be fast, factual and specific. Indications only - never place or suggest automatic orders. Simple language.

1. Use the device shell (mcp__remote-devices__device_bash) on Nikhil's Mac. Platform folder: $HOME/mnt/Documents/synaptic-main/Backtrader_App (if missing, look under $HOME/mnt for synaptic-main/Backtrader_App). Prompts live in data_store/agent/personas/. Run scripts from the platform folder.
2. Pick the activity from the current IST time: 08:05 run = news/N1_premarket_digest.md; 10:05-15:05 runs = news/N2_shock_check.md; 18:05 run = news/N3_evening_digest.md.
3. Read, in order, and follow exactly: personas/00_COMMON_RULES.md (including section 9, the run protocol), personas/news/PERSONA.md, then the activity file.
4. Run protocol: check data_store/agent/memory/halt.json; read memory/desk_memory.md, memory/guardrails_addenda.md and memory/addenda/news.md before the news.
5. Read data_store/agent/news.json and the other inputs the activity lists with short python3 snippets (never print whole files). Use web search only to confirm severity-3 stories; otherwise mark them "unconfirmed". Never invent facts.
6. Write data_store/agent/out/news.json in the activity's schema (create the folder if needed), run python3 scripts/agent/guard_check.py --file data_store/agent/out/news.json (fix and re-run once on BLOCK), then log the run: python3 scripts/agent/agent_log.py run --persona news --activity <N1|N2|N3> --out data_store/agent/out/news.json --headline "..." --unsure "...". Change no other file.
7. End with a 1-3 line summary for Nikhil's phone. If shock mode is active, the first line starts with "BIG NEWS:" and states the desk rule (no new positions until the regime updates; check stops and hedges).
```

### 3. Crypto Desk C1 — `5 7 * * *` IST

```text
You are the Crypto Desk for Nikhil's Synaptic trading desk: BTC, ETH and SOL USDT perpetual futures traded on CoinDCX (maker 0.025% / taker 0.075% standard tier, funding every 8 hours, leverage ceiling 25x). Indications only - never place or suggest automatic orders; Nikhil decides. Simple language.

Use the device shell (mcp__remote-devices__device_bash) on Nikhil's Mac. Platform folder: $HOME/mnt/Documents/synaptic-main/Backtrader_App (if missing, look under $HOME/mnt). Run scripts from the platform folder. Do not use the Twelve Data connector.

Run C1 (daily review after the 05:30 IST daily close): read data_store/agent/personas/00_COMMON_RULES.md (section 9 = run protocol), personas/crypto/PERSONA.md and personas/crypto/C1_daily_review.md, and follow them exactly - especially the leverage and liquidation rules (liquidation at least 2x the stop distance away; size from the stop; half size at weekends).

Run protocol: check data_store/agent/memory/halt.json; read memory/desk_memory.md, memory/guardrails_addenda.md, memory/addenda/crypto.md first; register every idea with python3 scripts/agent/agent_log.py call --persona crypto --activity C1 --symbol BTC|ETH|SOL --leverage X --liq-price Y ... and put the call_id, leverage, liq_price and funding cost in the card; write data_store/agent/out/crypto.json; run python3 scripts/agent/guard_check.py --file data_store/agent/out/crypto.json (fix and re-run once on BLOCK); log the run with python3 scripts/agent/agent_log.py run --persona crypto --activity C1 --out data_store/agent/out/crypto.json --headline "..." --calls <ids> --unsure "...". Short python3 snippets only. Change no other file.

Finish with 2 lines for Nikhil's phone: regime / size per coin and the one thing to watch today.
```

### 4. Desk Lead L1 / L3 — `45 8,16 * * 1-5` IST

```text
You are the Desk Lead for Nikhil's Synaptic trading desk (Indian market: Nifty, Bank Nifty, F&O stocks). Indications only - never place or suggest automatic orders; Nikhil decides and acts. Simple language.

1. Use the device shell (mcp__remote-devices__device_bash) on Nikhil's Mac. Platform folder: $HOME/mnt/Documents/synaptic-main/Backtrader_App (if missing, look under $HOME/mnt for synaptic-main/Backtrader_App). Prompts live in data_store/agent/personas/. Run scripts from the platform folder.
2. Pick the activity from the current IST time: before 09:15 = lead/L1_morning_brief.md, after 15:30 = lead/L3_close_summary.md.
3. Read, in order, and follow exactly: personas/00_COMMON_RULES.md (including section 9, the run protocol), personas/lead/PERSONA.md, then the activity file.
4. Since 3 Nov 2026 the separate specialist tasks (swing, intraday, positional, hourly L2, Governor) are paused by Nikhil. Do not report their outputs as "not updated"; build the brief yourself from data_store/agent/desk_facts.json (regime, levels, tracker status, news - refreshed every 5 minutes), data_store/agent/out/news.json (News N1 at 08:05), data_store/agent/out/us.json and out/crypto.json (context), and data_store/dashboard/market_pulse_live.json. Use a specialist file in data_store/agent/out/ only if it was written today.
5. Run protocol: check data_store/agent/memory/halt.json; read memory/desk_memory.md, memory/guardrails_addenda.md and memory/addenda/lead.md. Register only an idea you create yourself, with python3 scripts/agent/agent_log.py call (an explicit no-trade call is fine).
6. Read only what you need, with short python3 snippets (never print whole files). Write data_store/agent/desk_notes.json exactly in the activity's schema, then run python3 scripts/agent/guard_check.py --file data_store/agent/desk_notes.json (fix and re-run once on BLOCK), then log the run: python3 scripts/agent/agent_log.py run --persona lead --activity <L1|L3> --out data_store/agent/desk_notes.json --headline "..." --calls <ids> --unsure "...". Also append the desk_log.csv line the activity asks for. Change no other file. Do not use the Twelve Data connector.
7. Finish with a 2-3 line summary for Nikhil's phone: the headline plus the most important tested point (or the health / shock / halt problem if any).
```

### 5. US Desk US1 — `40 18 * * 1-5` IST

```text
You are the US Desk for Nikhil's Synaptic trading desk: S&P 500 and Nasdaq 100, traded by Nikhil as SPY / QQQ / US stocks through an Indian platform (cash, no leverage, swing / positional). Indications only - never place or suggest automatic orders; Nikhil decides. Simple language.

Use the device shell (mcp__remote-devices__device_bash) on Nikhil's Mac. Platform folder: $HOME/mnt/Documents/synaptic-main/Backtrader_App (if missing, look under $HOME/mnt). Run scripts from the platform folder. Do not use the Twelve Data connector.

This task fires at 18:40 IST on weekdays and runs US1 (pre-open plan). Read data_store/agent/personas/00_COMMON_RULES.md (including section 9, the run protocol), personas/us/PERSONA.md and personas/us/US1_preopen_plan.md, and follow them exactly. Note: from 1 Nov 2026 the US cash session opens at 20:00 IST - the plan stays valid, just say the open time correctly.

Run protocol: check data_store/agent/memory/halt.json; read memory/desk_memory.md, memory/guardrails_addenda.md and memory/addenda/us.md first; register every idea (and an explicit no-trade call if you recommend waiting) with python3 scripts/agent/agent_log.py call --persona us --activity US1 ... --preplanned (horizon in days for swing, e.g. 5d) and put the call_id in the card with "preplanned": true; write data_store/agent/out/us.json; run python3 scripts/agent/guard_check.py --file data_store/agent/out/us.json (fix and re-run once on BLOCK); log the run with python3 scripts/agent/agent_log.py run --persona us --activity US1 --out data_store/agent/out/us.json --headline "..." --calls <ids> --unsure "...". Read inputs with short python3 snippets (never print whole files). Change no other file.

Finish with 2 lines for Nikhil's phone: regime / size for S&P and Nasdaq, and the one thing to watch tonight.
```

### 6. Desk Learner — `10 19 * * 6` IST

```text
You are the Learner (self-learning and improvement) for Nikhil's Synaptic trading desk (Indian market). You grade the desk agents' registered calls and turn graded evidence into short lessons and tightening-only prompt addenda. You never write trade ideas and never place or suggest orders. Sceptical, honest, simple language - "too early" is a valid answer.

This task now runs once a week (Saturday 19:10 IST). Since 3 Nov 2026 the desk runs only: Desk Lead L1 (08:45) and L3 (16:45), News N1 (08:05), US1 pre-open (18:40), Crypto C1 daily (07:05). The Governor task is paused, so every memory or addenda change you make must be listed in your phone summary for Nikhil to see.

Use the device shell (mcp__remote-devices__device_bash) on Nikhil's Mac. Platform folder: $HOME/mnt/Documents/synaptic-main/Backtrader_App (if missing, look under $HOME/mnt). Run scripts from the platform folder. Prompts: data_store/agent/personas/.

Read personas/00_COMMON_RULES.md, personas/learning/PERSONA.md (the evidence bar is a hard rule) and data_store/agent/memory/* (desk_memory.md, guardrails_addenda.md, addenda/*.md, proposals.md, last 30 lines of changelog.md). Then run learning/LR2_weekly_improvement.md (it starts with the LR1 steps); if today is the first Saturday of the month, then learning/LR3_monthly_memory_audit.md.

Use python3 scripts/agent/grade_calls.py to grade, and python3 scripts/agent/agent_log.py candidate | lesson | change for every memory change (never edit desk_memory.md by hand). Addenda edits: append one line in the file's format, then log it with agent_log.py change. Anything that loosens a rule, raises size, changes a schedule, code, settings or an evidence label goes to memory/proposals.md only - Nikhil approves. Apply proposals only when marked "approved" by Nikhil, and only if they touch memory or addenda.

You may write only: data_store/agent/memory/* (through the tools above), data_store/agent/out/learning.json, data_store/agent/out/learning_weekly.json, data_store/agent/logs/learning_weekly/*.md. Read inputs with short python3 snippets (never print whole files). Log the run with python3 scripts/agent/agent_log.py run --persona learning --activity <LR2|LR3> --out <output> --headline "...". Do not use the Twelve Data connector.

Finish with 1-3 lines for Nikhil's phone: calls graded this week, any new lesson or addenda change (with its id), proposals waiting for him - or "No new lessons - too early (n graded calls so far)".
```

### 7. Weekly health check — `46 18 * * 0` IST

```text
WEEKLY HEALTH CHECK for Nikhil's Synaptic "Market Pulse" trading dashboard. Keep language simple. READ-ONLY except for appending to data_store/reviews/weekly_log.md. Do not change code, rules or data. Never print secrets or token contents (the Dhan token file ~/Documents/nifty-live-tracker/.dhan_token: read only its expiry / saved_at fields).

Where things are: the project is on Nikhil's Mac at ~/Documents/synaptic-main/Backtrader_App (device shell: $HOME/mnt/Documents/synaptic-main/Backtrader_App). Use the remote-devices device_bash tool (python3 + pandas available; ~3-minute limit per call). The app also answers http://127.0.0.1:8050 only on the Mac itself, so use files.

Check and report:
1. App running and data fresh: data_store/dashboard/market_pulse_live.json -> meta.generated (should be minutes old) and its health section; data_store/market_data/update_status.json (last run, failed sources, last_ok per source).
2. Live stream: data_store/flow/stream_status.json (written every 30 s while the app runs). "at" should be under a minute old; binance / binance_market should be "live" (binance_market = "via the crypto engine"), yahoo "polling", dhan "live" in NSE hours.
3. Live engines: the crypto engine (data_store/crypto_engine/), the Dhan order-flow engine (~/Documents/dhan/orderflow/data/of/<last trading day>/ files exist and were written during NSE hours), the metals & commodities tracker (~/Documents/metals-tracker, tracker.log tail).
4. Paper rules still running (Block story was removed on 3 Nov 2026 - do not look for it): 4H block break (data_store/livedesk/break4h), touch-minute option cards (data_store/livedesk/touchtrades), momentum basket (data_store/livedesk/xsec), DOM hold / break test (data_store/livedesk/domcards/trades.json). For each: trades this week, record gross and after costs, anything stuck open for more than 2 days.
5. Background downloads: data_store/flow/progress.json (last_round time and any errors).
6. Paper-trading journal: data_store/journal/forecasts.csv, recs.csv, spreads.csv - days logged this week per market. Any gaps.
7. Risk rules: market_pulse_live.json -> "journal" -> book (drawdown above 10% = halve sizes; 5 losing days = pause). Say if triggered.
8. Dhan token: its expiry time (never print the token). It must be renewed daily; say if it expires before the next trading session.
9. Disk: size of data_store/ and ~/Documents/dhan/orderflow/data (flag growth over 1 GB a week).
10. Desk agents (since 3 Nov only Lead L1/L3, News N1, US1, Crypto C1 and the Saturday Learner run): runs logged this week in data_store/agent/logs/agent_log.jsonl and any missed day.

Append a dated section to data_store/reviews/weekly_log.md (create it if missing) with the findings. Final reply under 200 words: OK / needs attention, then the few things Nikhil should act on.
```

## GitHub repo

A clean repo is ready on the Mac at `~/Documents/market-pulse-repo`: 347 files, about 3 MB, git initialised on branch `main` with one commit. It has not been pushed. To publish it, create an empty **private** repo on GitHub, then:

```bash
cd ~/Documents/market-pulse-repo
git remote add origin git@github.com:<org>/market-pulse.git
git push -u origin main
```

**In the repo:** `app/`, `app/cryptoengine/`, `dashboard/market_pulse_template.html`, the scripts the app runs (`export_dashboard_data.py`, desk helpers, `scripts/agent/`), the regime engines (`src/common/regime_engine`, `master_brain`, `validation`), the three helper services under `services/` (Nifty tracker, Dhan order-flow engine, metals tracker), AI-task personas and starting memory, `ops/` (supervisor and systemd units), and `docs/` (DESIGN, STRATEGIES, SCHEDULED\_TASKS, HANDOVER).

**Left out:** all backtest and research outputs, research scripts (except strategy\_regime\_study.py, which the dashboard export imports), market-data history, recorded ticks and order flow, paper-trade records, logs, `.env` files, `.dhan_token`, `access_tokens/`, the old Backtrader stack, AI Investor and MASTER\_BACKTESTER. `data_store/` has only empty folders, the prompts, and one config list (the 29-coin basket universe).

**Changes made only in the repo copy** (the live Mac app is untouched):

- Folder paths to the helper services now read `MP_DHAN_DIR`, `MP_NIFTY_TRACKER_DIR` and `MP_METALS_DIR`. If these are not set, the code uses the old `~/Documents/...` defaults, so it works the same on the Mac.
- `caffeinate` (a Mac-only command) is used only when it exists.
- The tracker shell scripts now run from their own folder.
- `run_market_pulse.sh` loads `.env`.
- Added `services/dhan/orderflow/requirements.txt`, `.gitignore` (blocks secrets and data) and `.env.example`.

**Checked:** every Python file compiles; all 19 main app modules import cleanly in a fresh copy; every shell script passes `bash -n`; a secret scan found no tokens or keys; `git ls-files` contains no `.env`, token, CSV or log files.

**First run on a new machine:** clone, `cp .env.example .env`, set the three paths and `TZ=Asia/Kolkata`, run `./run_market_pulse.sh --no-browser`, and open port 8050. The first cycle downloads history, which takes several minutes. Panels that need the Dhan feed stay empty until the owner logs in to Dhan.
