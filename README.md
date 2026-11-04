# Market Pulse

A live market dashboard for Indian, US, crypto, metals and commodity markets: regime, hourly verdict, blocks and levels, order flow, paper-trade rules, catalyst news and a weekly global narrative.

> **Paper trading and indications only.** Nothing in this repo places orders. Not financial advice. Forward results never change rules automatically.

## What is in this repo

```
app/                 Market Pulse server (stdlib HTTP, port 8050) + live desk, stream hub, news, levels, paper rules
app/cryptoengine/    In-process crypto engine (Hyperliquid + Binance feeds, footprint, setups, paper book)
dashboard/           market_pulse_template.html – the whole front end (HTML + CSS + JS, no build step)
scripts/             export_dashboard_data.py (rebuilds the dashboard JSON every cycle), india/global desk helpers
scripts/agent/       Helpers the scheduled AI tasks call (agent_log, guard_check, grade_calls, narrative_check)
src/common/          Regime engines (regime_engine/) and market definitions (master_brain/)
services/nifty-tracker/   Nifty / Bank Nifty live tracker (Dhan feed, port 8899) + Dhan token keep-alive
services/dhan/orderflow/  Dhan order-flow engine (ticks, 20-level book, footprint) – writes data/of/<day>/
services/metals-tracker/  Gold / silver / energy tracker on Hyperliquid xyz perps (port 8897)
data_store/agent/personas/  Prompt specs the scheduled AI tasks follow
data_store/agent/memory/    Starting desk memory for those tasks
ops/                 Supervisor script and example systemd units
docs/                HANDOVER (link), DESIGN, STRATEGIES, SCHEDULED_TASKS
```

**Left out on purpose:** all backtest and research results, market-data history, recorded ticks / order flow, paper-trade records, logs, broker tokens and `.env` files, the older Backtrader trading stack, and the AI Investor product. `data_store/` holds only empty folders (`.gitkeep`), the AI-task prompts and one config list (`data_store/research/alts1m/established.txt`, the crypto basket universe). Market data is downloaded again on first run.

## Requirements

- Linux (Ubuntu 22.04+ recommended) or macOS; Python **3.11+** (runs today on 3.9).
- Outbound HTTPS to Yahoo Finance, Binance futures, Hyperliquid, NSE, RSS news feeds, Dhan.
- A Dhan account with data API access for Nifty / Bank Nifty order flow (optional – the rest works without it).
- About 2 GB disk to start; data grows ~1 GB a month (see the handover doc).

## Run on a new machine

```bash
git clone <your-repo-url> market-pulse && cd market-pulse
cp .env.example .env          # fill in paths; broker secrets go in the services' own .env files (never commit)
./run_market_pulse.sh --no-browser --port 8050 --every 5
# open http://127.0.0.1:8050
```

First start creates `.venv` and installs `app/requirements.txt`. The first cycle downloads market history and takes several minutes; panels show "—" until their data lands.

Optional services (each has its own `requirements.txt` and venv):

```bash
# Nifty tracker + Dhan keep-alive (needs Dhan login: python -m src.dhan_auth, owner only)
cd services/nifty-tracker && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./start-live.sh            # weekdays 09:00–15:25 IST

# Dhan order-flow engine (reads DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN from services/dhan/.env)
cd services/dhan && python3 -m venv venv && venv/bin/pip install -r orderflow/requirements.txt
cd orderflow && ../venv/bin/python -u of_engine.py

# Metals tracker
cd services/metals-tracker && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && ./run_forever.sh
```

`ops/start_desk.sh` keeps all of these up (checks every 60 s). For a server, use the units in `ops/systemd/`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MP_DHAN_DIR` | `~/Documents/dhan` | Folder that contains `orderflow/` (and `venv/`, `.env`) |
| `MP_NIFTY_TRACKER_DIR` | `~/Documents/nifty-live-tracker` | Nifty tracker folder (holds `.dhan_token`, `config.yaml`) |
| `MP_METALS_DIR` | `~/Documents/metals-tracker` | Metals tracker folder |

In this repo set them to `services/dhan`, `services/nifty-tracker` and `services/metals-tracker` (absolute paths). `run_market_pulse.sh` loads `.env` automatically.

## Ports

8050 dashboard · 8051 intraday API · 8052 live stream (SSE) · 8897 metals tracker · 8899 Nifty tracker. The server binds to **127.0.0.1 only** and has no login. Put a reverse proxy with TLS and authentication in front before exposing it.

## Docs

- Full production handover (architecture, run book, gaps): https://claude.ai/artifact/NsW1ZwEwFejFjTkL9fjFDF
- `docs/DESIGN.md` – colours, fonts, layout, components, refresh cadence
- `docs/STRATEGIES.md` – every engine and paper rule, with exact thresholds
- `docs/SCHEDULED_TASKS.md` – the AI task prompts, verbatim, with schedules

## Secrets

Never commit tokens. `.gitignore` blocks `.env`, `.dhan_token`, `.kite_token` and `access_tokens/`. Run a secret scanner (e.g. `gitleaks detect`) before every push.
