# Market Pulse – Design System

The whole front end is one file: `dashboard/market_pulse_template.html` (~290 KB, plain HTML + CSS + vanilla JS, no build step, no framework). The server injects the latest JSON into it at `/`.

## Colour tokens

Defined on `:root`, with a dark set under `prefers-color-scheme: dark` and `[data-theme="dark"]`. Always use the tokens, never raw hex.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | #EEF2F5 | #0A1016 | Page background |
| `--surface` | #FFFFFF | #111A22 | Cards |
| `--surface-2` | #F6F8FA | #16212B | Chips, inner panels |
| `--ink` | #13212E | #E3E9EF | Main text |
| `--muted` | #5B6979 | #97A5B3 | Secondary text |
| `--faint` | #8C98A5 | #6E7C8A | Captions |
| `--line` | #DAE1E7 | #22303C | Borders |
| `--accent` / `--accent-soft` | #0E6C73 / #E2F1F1 | #43B4B4 / #12302F | Selected chip, focus ring, links |
| `--bull` / `--bull-soft` | #1E8A4F / #E3F3EA | #3CC27C / #12301F | Up, buy-side, positive |
| `--bear` / `--bear-soft` | #C23A31 / #F8E6E4 | #EF625A / #3A1816 | Down, sell-side, negative |
| `--neutral` / `--neutral-soft` | #7D8894 / #EDF0F3 | #8F9AA6 / #1C2731 | Flat, mixed, no data |
| `--wait` / `--wait-soft` | #C77D05 / #FBF0DC | #E9A23B / #33260F | "Wait" verdicts, stale warnings |
| `--hero`, `--hero-2` | #0D1923, #13232F | #081118, #0F1C26 | Dark hero band (gradient 160°) |
| `--hero-ink`, `--hero-muted`, `--hero-line` | #E8EEF3, #93A3B2, #22394A | same family | Text and lines on the hero |

**Chart line colours (block chart dashed levels):** VAH / VAL / POC purple `#8B7CF6`, VWAP amber `#E8A33C`, IB high / low blue `#2F9BD6`, PDH / PDL / PWH / PWL / PMH / PML grey (`--faint`). The chart is a horizontal price axis. Blocks are bands on it: strong = 22 px tall at 0.85 opacity, medium = 14 px at 0.6, weak = 7 px at 0.4. Support uses `--bull`, resistance `--bear`, a block that contains price ("IN") uses `--wait`. Strong and medium blocks get range labels (bold for strong, ★ for a weekly / monthly star level), alternating above / below. Price is a white dot with a "now <price>" label; the 5-day range is an accent bar at 0.35 opacity. Dashed level lines are 1.4 px, dash 4/3, labelled above the axis.

## Typography

Google Fonts: **Archivo** 500–800 (display), **IBM Plex Sans** 400–600 (body), **IBM Plex Mono** 400–600 (numbers).

| Role | Font | Size / weight |
|---|---|---|
| h1 (page title) | Archivo | 30 px / 800 |
| h2 (card title) | Archivo | 17 px / 700 |
| h3 | Archivo | 14 px / 700 |
| Body | IBM Plex Sans | 14 px / 1.45 line height |
| Eyebrow / labels | IBM Plex Sans | 12 px / 600, muted |
| Numbers, prices, tables | IBM Plex Mono | tabular numerals (`font-variant-numeric: tabular-nums`) |
| Hero verdict word | Archivo | clamp(40px, 6vw, 64px) / 800 |

Labels use **Title Case**. Language is plain and short ("Buy on dips", "Into strong support – wait").

## Layout

- Page wrapper `.wrap`: max width 1480 px, centred, 20 px side padding, vertical flex with 18 px gap.
- Cards `.card`: surface background, 1 px `--line` border, radius 10 px (`--r`), padding 16 px.
- Hero: radius 14 px, padding 22 px, 2-column grid (400 px visual + text), collapses to one column under 1000 px.
- Breakpoints in use: 1250, 1150, 1100, 1000, 820, 760, 700, 640, 560, 520 px. Under 640 px everything stacks; tables become scroll-free compact rows.
- Pills `.pill`: fully rounded, 12 px / 600, coloured by `.p-bull / .p-bear / .p-neutral / .p-wait / .p-accent`.
- Chips `.chip` (market and timeframe pickers): 8 px radius, selected = accent fill with white text, `aria-pressed`.
- Focus: 2 px accent outline on `:focus-visible`.
- Stale data: `.stale` dims a card to 50 % and shows a "Last session" / age stamp badge.

## Page structure

1. Header: title, data stamps (last build time, stream status), theme toggle.
2. Tab rail: Overview · Indian Market · US Market · Crypto · Metals · Commodities · Intraday / Live Desk.
3. Each market tab, top to bottom: **Now strip** (one line: price, regime, verdict, next event) → **Now · Verdict** card → **block line chart** with dashed levels → Order Flow (Indian: last 12 candles, any timeframe) → Levels & Blocks → paper-trade cards → **tab catalyst news** → extra views.
4. Overview: Global Indices strip (tiles, 1 s live) → Global Narrative · This Week → All Markets table (Latest Price beside the name, bold Regime column centred inside a transparent rounded band, narrow Impact scores, no horizontal scroll) → News & Events with News Sentiment panel on the right → Regime History vs Nifty · Last 90 Sessions.

## Components to rebuild faithfully

| Component | Notes |
|---|---|
| Global Indices tile | Name, live price (mono), day % change coloured bull / bear, source tag (perp / Yahoo) |
| Now strip | Single row, ellipsis on overflow, same facts as the verdict card (one number per fact across the page) |
| Verdict card | Verdict word + tone colour, one-line reason, nearest blocks above / below with strength, OI note (Nifty / Bank Nifty) |
| Block line chart | Inline SVG: price line, block bands, dashed level lines with right-edge labels; levels only drawn when near price (VA / POC / VWAP / IB within 4 %, others within 2 %) |
| Order-flow footprint | Last 12 candles, bid × ask per price row, delta row, legend folded |
| Regime band | Transparent rounded rectangle behind the whole Regime column; regime text bold and coloured |
| News sentiment | Score −10…+10 for All / India / Global, label Positive (≥2) / Mixed / Negative (≤−2), top movers list |
| Tab catalyst list | Headline, catalyst label, direction arrow (up / down / either), strength dots, source + age |
| Paper-trade card | Rule name, entry / stop / target, R and bps gross then net, "PAPER" tag |

## Refresh cadence

| Data | How | Every |
|---|---|---|
| Global indices, live prices | Server-Sent Events from :8052 | 1 s |
| Live desk cards, DOM | `/api/livedesk/*` polling | 2–15 s |
| Verdicts, levels, blocks | `/api/livedesk/*` polling | 30–60 s |
| Main dashboard JSON | `/api/pulse` | 5 min (worker cycle) |
| Status ping | `/api/status` | 10 s |
| Tab news | built server-side | 10 min |
| Global narrative facts / story | API / scheduled task | 30 min / daily 07:17 IST |

## Rules for the UI team

- Paper / indications only: never add order buttons or broker links.
- Every number must come from data; show "—" when missing, never a guess.
- One fact = one number on screen (the same price or verdict must not differ between cards).
- Keep both themes; test at 375 px wide.
