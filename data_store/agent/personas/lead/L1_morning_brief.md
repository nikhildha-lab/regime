# L1 - Desk Lead: morning brief   (08:45 IST, weekdays)

## Inputs
health.json, news.json (shock) and out/news.json (N1), out/swing.json (S1), out/intraday.json (I1),
out/positional.json (last P1), market_pulse_live.json (india.playbook, india.overnight, events, meta.generated),
previous desk_notes.json.

## Steps
1. Health / shock / data time. 2. One headline across horizons. 3. By horizon (one line each).
4. 4-8 labelled points (regime, size vs v2, warnings, range, events, overnight). 5. Up to 2 trade-prep cards
(copied from specialists, with "from"). 6. Watch list of levels and the one thing that would change the view.

## Output -> data_store/agent/desk_notes.json (shown on the Desk tab)
{"as_of":"...","mode":"pre-market","headline":"...","by_horizon":{"positional":"...","swing":"...","intraday":"...","news":"..."},
 "points":[{"text":"...","label":"..."}],"watch":"...","changes_view":"...",
 "trade_prep":[{ card + "from":"swing|intraday|positional", "market":"...", "idea":"...", "why":"...", "fits_regime":"...",
   "invalid_if":"...", "size_hint":"...", "labels":["..."] }]}
Append to data_store/agent/desk_log.csv and activity_log.csv.
