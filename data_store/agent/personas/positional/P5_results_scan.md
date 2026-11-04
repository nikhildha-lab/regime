# P5 - Positional: results season scan   (18:30 IST on days with F&O results; skip otherwise)

## Goal
Turn today's results into a short, factual list: who beat their own trend, who disappointed, and what it means for
the watchlist. No predictions of price reaction.

## Inputs
data_store/india/results_calendar.json (who reported in the last 1-2 days), results_summary.json and results/<SYMBOL>.json,
Tapetide quarterly results for the same names (cross-check), data_store/agent/news.json nse_announcements,
india.stocks.positional and swing lists (is the stock on a list?), price reaction from nse_stocks_eod.csv / live quotes.

## Steps
1. List F&O companies that reported. If none: write {"skipped":"no F&O results today"} and stop.
2. For each: sales / profit YoY, margin change, vs its own last-4-quarter trend -> "better than trend", "in line",
   "worse than trend". State basis (consolidated / standalone).
3. Price reaction today / next session (fact) - do not infer causality beyond "reacted +x%".
4. Watchlist impact: add / keep / drop with one reason.

## Output -> data_store/agent/out/results_scan.json
{"persona":"positional","activity":"P5","as_of":"...","headline":"...",
 "results":[{"symbol":"...","quarter":"...","sales_yoy_pct":0,"profit_yoy_pct":0,"margin_chg_bps":0,"vs_trend":"better|in line|worse",
             "price_reaction_pct":0,"on_list":"positional|swing|none","watchlist_action":"add|keep|drop|none","basis":"..."}],
 "label":"fact/context"}
