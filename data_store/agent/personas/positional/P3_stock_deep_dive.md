# P3 - Positional: stock deep dive   (on request: "deep dive <SYMBOL>")

## Goal
A disciplined, source-checked one-pager on one stock: business trend, quality, valuation context, ownership, price
strength and the plan - so Nikhil can decide in 2 minutes.

## Inputs
- NSE: data_store/india/results/<SYMBOL>.json (last ~5 quarters), results_summary.json, results_calendar.json
- Tapetide connector (if available): quarterly results (13q), annual statements (up to 12y), ratios (P/E, P/B, EV/EBITDA,
  ROE, ROCE, debt/equity, interest cover, OCF/PAT), shareholding history (promoter, FII, DII, MF), promoter pledges
- Price: stocks_data.csv + data_store/market_data/nse_stocks_eod.csv (close, 50 / 200-day averages, 52-week high/low,
  6-month return); india.stocks lists; india.sectors.rows for its sector
- News: data_store/agent/out/news.json stock_flags, data_store/agent/news.json nse_announcements

## Steps
1. Identity check: symbol, sector, F&O lot (if in universe). Results date ahead?
2. Business trend (fact): last 5 quarters sales / net profit / margin; YoY growth; standalone vs consolidated stated.
   Cross-check Tapetide vs NSE on the latest quarter; flag mismatches > 2%.
3. Quality (fact -> read): ROE / ROCE vs 5-year average, debt / equity, OCF vs PAT (earnings quality), interest cover.
4. Valuation context (context): P/E and EV/EBITDA vs own 5-year range (percentile). Not a timing signal.
5. Ownership (fact): promoter % change over 4 quarters, pledged %, FII / MF trend.
6. Price (context / watch): vs 50 / 200-day, 6-month relative strength vs Nifty, distance to 52-week high, sector quadrant.
7. Verdict: Accumulate on pullback / Wait (reason) / Avoid (reason), with entry zone, invalidation, horizon, size hint.
8. Red flags list (any of): falling margins 3 quarters, rising debt, pledge increase, promoter selling, OCF << PAT,
   auditor / governance announcements.

## Output -> data_store/agent/out/deepdive_<SYMBOL>.json
{"persona":"positional","activity":"P3","symbol":"...","as_of":"...","sources":{"nse_results":"quarter_end","tapetide":true|false},
 "verdict":"Accumulate on pullback|Wait|Avoid","headline":"...",
 "business":{"quarters":[{"q":"...","sales":0,"profit":0,"margin_pct":0}],"yoy_sales_pct":0,"yoy_profit_pct":0,"basis":"consolidated|standalone"},
 "quality":{"roe":0,"roce":0,"de":0,"ocf_to_pat":0},"valuation":{"pe":0,"pe_5y_pct":0},
 "ownership":{"promoter_pct":0,"promoter_chg_4q":0,"pledge_pct":0,"fii_trend":"...","mf_trend":"..."},
 "price":{"vs_200dma_pct":0,"rs_6m_vs_nifty_pct":0,"from_52w_high_pct":0,"sector_quadrant":"..."},
 "red_flags":["..."],"plan":{ trade-prep card },"source_mismatches":["..."]}
Use null for anything not available. Never fill gaps from memory.
