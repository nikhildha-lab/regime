"""BUILDUP - round 6: sell CALLS instead of shorting the future (2026-11-08, Nikhil: "we keep futures .. we try call selling just for
experimentation .. returns will be less but worth experimenting").

Same signals as BuildUp v1 (and the v2 subset). Same timing: in at the next session's open, out at the close of the 5th session, and out
early on any day the FUTURE's high reaches entry + 5 % (the v1 stop). All choices below were fixed before any result was seen.
Option = stock option, expiry = nearest with >= 10 calendar days left at entry (no expiry inside the hold). Strikes from the signal day's
underlying close (known at decision time):
   ATM     sell the strike nearest the underlying
   OTM     sell the first strike >= underlying x 1.03
   SPREAD  sell ATM, buy the first strike >= underlying x 1.05 (bear call spread, risk capped)
Fills (daily bhavcopy only - no intraday option prices):
   entry   the option's OPEN on the entry day, only if it traded that day (volume > 0); otherwise the trade is counted as "no fill"
   exit    the option's CLOSE on the exit day if it traded, else NSE's settlement price (counted as stale)
   stop    bought back at the option's HIGH of the stop day (worst case - real fills would usually be better)
P&L per trade in % of the underlying (so it compares directly with the futures numbers), gross and after costs:
   costs = 3 % of the premium round trip (half-spread x 2 + STT 0.1 % + charges), plus a 6 % stress case for thin names.
Futures on the SAME trades are shown alongside (0.06 % costs).
Data: data_store/research/buildup_opts/opt_<day>.csv.gz (app/buresearch.py, NSE bhavcopy).
Run: python3 scripts/research/fno_flow_research6.py
"""
from __future__ import annotations

import importlib.util
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OPT = os.path.join(ROOT, "data_store", "research", "buildup_opts")
HOLD, STOP = 5, 5.0


def t(x):
    x = pd.Series(x).dropna(); return x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else np.nan


def load(name):
    sp = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", "research", f"{name}.py"))
    m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m


_O = {}


def opts(day):
    if day not in _O:
        fp = os.path.join(OPT, f"opt_{day}.csv.gz")
        _O[day] = pd.read_csv(fp).set_index(["sym", "expiry", "strike", "cp"]).sort_index() if os.path.exists(fp) else None
    return _O[day]


def leg(day, sym, exp, k):
    o = opts(day)
    if o is None or (sym, exp, k, "CE") not in o.index:
        return None
    return o.loc[(sym, exp, k, "CE")]


def run(r4, mode="worst"):
    """mode: worst = stop-day fills at the day's high / low (default); close = stop-day fills at the close; nostop = hold 5 sessions, no stop."""
    P = r4.P[r4.P.ret.abs() <= 20]
    base = P[(P.oi_chg >= 10) & (P.ret <= -2)].sort_values(["day", "oi_chg"], ascending=[True, False]).groupby("day").head(5)
    sig = base[base.clv < -0.5]
    fut = r4.simulate(sig, HOLD, STOP).set_index(["day", "sym"])
    days, di = r4.days, r4.di
    rows = []
    for _, s in sig.iterrows():
        i = di[s.day]
        if i + HOLD >= len(days) or (s.day, s.sym) not in fut.index:
            continue
        f = fut.loc[(s.day, s.sym)].copy(); held = int(f.held)
        if mode == "nostop":
            held = HOLD; f["why"] = "time"
        ed, xd = days[i + 1], days[i + held]
        und = s.und if pd.notna(s.get("und")) else s.close
        o = opts(ed)
        if o is None:
            continue
        try:
            ch = o.xs(s.sym, level="sym")
        except KeyError:
            rows.append({"day": s.day, "sym": s.sym, "status": "no options"}); continue
        exps = sorted(e for e in ch.index.get_level_values("expiry").unique() if (pd.Timestamp(e) - pd.Timestamp(ed)).days >= 10)
        if not exps:
            rows.append({"day": s.day, "sym": s.sym, "status": "no expiry"}); continue
        exp = exps[0]; ks = np.array(sorted(ch.xs(exp, level="expiry").index.get_level_values("strike").unique()))
        atm = ks[np.abs(ks - und).argmin()]; otm = ks[ks >= und * 1.03]; wing = ks[ks >= und * 1.05]
        res = {"day": s.day, "sym": s.sym, "status": "ok", "fut": f.pnl, "why": f.why, "und": und}
        for nm, short, long_ in (("ATM", atm, None), ("OTM", otm[0] if len(otm) else None, None), ("SPREAD", atm, wing[0] if len(wing) else None)):
            if short is None or (nm == "SPREAD" and long_ is None):
                continue
            legs = [(short, -1)] + ([(long_, +1)] if long_ is not None else [])
            ent, ext, prem, stale, ok = 0.0, 0.0, 0.0, False, True
            for k, q in legs:
                a, b = leg(ed, s.sym, exp, k), leg(xd, s.sym, exp, k)
                if a is None or b is None or not (a.vol > 0 and a.open > 0):
                    ok = False; break
                if f.why == "stop" and mode == "worst":
                    px = b.high if (b.vol > 0 and b.high > 0) else b.settle            # worst case for the short leg
                    if q > 0:
                        px = b.low if (b.vol > 0 and b.low > 0) else b.settle            # and for the long wing
                else:
                    px = b.close if b.vol > 0 else b.settle; stale |= not (b.vol > 0)
                ent += -q * a.open; ext += -q * px; prem += a.open
            if not ok:
                res[nm] = np.nan; res[nm + "_nofill"] = True; continue
            g = (ent - ext) / und * 100                                             # credit kept, % of the underlying
            res[nm] = g; res[nm + "_net3"] = g - 0.03 * prem / und * 100; res[nm + "_net6"] = g - 0.06 * prem / und * 100
            res[nm + "_credit"] = ent / und * 100; res[nm + "_stale"] = stale
        rows.append(res)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    import sys
    r4 = load("fno_flow_research4"); MODE = sys.argv[1] if len(sys.argv) > 1 else "worst"; R = run(r4, MODE); half = r4.half
    print(f"mode: {MODE}")
    r5 = pd.read_csv(os.path.join(ROOT, "data_store", "research", "buildup_round5_trades.csv"), usecols=["day", "sym", "S3", "S5"])
    R = R.merge(r5, on=["day", "sym"], how="left")
    R["v2"] = ~((R.S5.astype(str) == "True") | (R.S3 == "broken"))
    ok = R[R.status == "ok"]
    print(f"BuildUp v1 signals with option data: {len(ok)} (no options listed {(R.status=='no options').sum()}, no expiry {(R.status=='no expiry').sum()})")
    for nm in ("ATM", "OTM", "SPREAD"):
        x = ok.dropna(subset=[nm]); nf = ok[nm].isna().sum()
        print(f"\n{nm}: filled {len(x)} (no trade at the open in {nf}) · stale exits {x[nm+'_stale'].astype(bool).mean()*100:.0f}% · avg credit {x[nm+'_credit'].mean():.2f}% of the stock")
        for lab, d in (("v1", x), ("v2", x[x.v2])):
            h = d.day < half; fu = d.fut
            print(f"  {lab}: n {len(d):4d} | option gross {d[nm].mean():+.2f}% (t {t(d[nm]):+.1f}) net@3% {d[nm+'_net3'].mean():+.2f}% (t {t(d[nm+'_net3']):+.1f}) "
                  f"net@6% {d[nm+'_net6'].mean():+.2f}% | win {100*(d[nm+'_net3']>0).mean():.0f}% | halves net@3% {d[nm+'_net3'][h].mean():+.2f}/{d[nm+'_net3'][~h].mean():+.2f} "
                  f"| worst {d[nm+'_net3'].min():+.1f}% || futures same trades {fu.mean():+.2f}% (t {t(fu):+.1f}) win {100*(fu>0).mean():.0f}% worst {fu.min():+.1f}%")
            yr = d.groupby(d.day.str[:4])[nm + "_net3"].mean().round(2).to_dict(); print(f"      by year net@3%: {yr}")
    R.to_csv(os.path.join(ROOT, "data_store", "research", f"buildup_round6_calls_{MODE}.csv"), index=False)
