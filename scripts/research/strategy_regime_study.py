"""Strategy x regime study (2026-10-22).

Which existing strategies (MASTER_BACKTESTER trade logs) do better or worse in which market conditions,
and does a regime overlay (skip / resize) built on OLD data still help on NEW data?

Trades: MASTER_BACKTESTER/results/*/trades_*.csv (NIFTY, BANKNIFTY, BTC, ETH, SOL, GOLD; costs included),
        Nifty / Bank Nifty options books, US stock books (experiments/us_stocks). Every trade is scored in
        units of its own leg's P&L spread (u = net P&L / std of that leg's trade P&L), so legs of different
        size can be pooled. Positive u = profit.
Conditions (all known at the PREVIOUS daily close - no look-ahead):
  trend      Trending (ADX14 >= 25) / Sideways (ADX14 < 18) / Mixed
  direction  above / below the 200-day average
  vol        realised 20-day vol vs its own last year: Low (< 33rd pct) / Normal / High (> 67th pct)
  ivol       implied vol (India VIX, VIX, BTC/ETH DVOL) vs last year: Low / Normal / High
  ivchg      implied vol 5-day change: Rising (> +10%) / Falling (< -10%) / Flat
  shock      US VIX up > 15% on the day before entry (global risk-off day)
  regime     our exposure regime engine label (Bull_Trend / Bear_Trend / Range / Compression / High_Volatility)
  weekday, expiry (India: NIFTY weekly expiry day), month_edge (first / last 2 trading days)
Pre-registered hypotheses (by strategy family):
  H1 trend-following does better Trending than Sideways     H2 mean-reversion does better Sideways than Trending
  H3 short-vol options do worse when implied vol is Rising   H4 directional trades do better with the 200-day direction
Split: train before 2024-07-01, test from 2024-07-01 (same split as earlier desk tests).
Overlay test: for each family, the worst condition cells on TRAIN (avg u < 0, n >= 40) are skipped on TEST;
compared with the base and with 300 shuffled-label runs (luck check).
Outputs -> data_store/research/strategy_regime/
"""
import glob, json, os, re, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from src.common.regime_engine import exposure_regime as XR
MB = os.path.abspath(os.path.join(ROOT, "..", "..", "MASTER_BACKTESTER"))
OUT = os.path.join(ROOT, "data_store/research/strategy_regime")
DEEP = os.path.join(ROOT, "data_store/market_data/yahoo_daily_deep.csv")
SPLIT = pd.Timestamp("2024-07-01")
RNG = np.random.default_rng(7)

# ---------- strategy families (from STRATEGY_TAXONOMY.md styles) ----------
FAM = {
 "trend": r"carver_(ewmac|breakout|combined|accel|skew)|escape|heikin|dual_thrust|ema5|ema_|psar|supertrend|trend|momentum|martin_luke|gold_impulse|darvas|minervini|ma18|ao_saucer|ensemble|combined_algo|wave|vortex|channel|breakout|donchian|turtle|adx|ichimoku|keltner|chandelier|weinstein|ptj|apex|peer|pivot_ext|marci|8020|okala",
 "meanrev": r"meanrev|pbd|cpr|rsi|smt|jadecap|blackbox|reclaim|liquidity|ict|fvg|value|bos|pullback|ema200_retest|vwap|tori|reversal|fade",
 "short_vol": r"condor|straddle|fly|ratio|delta_hedged|calendar|volcano|owl|odte",
}
def family(s):
    s = s.lower()
    for k in ("short_vol", "meanrev", "trend"):
        if re.search(FAM[k], s):
            return k
    return "other"

# ---------- market data ----------
deep = pd.read_csv(DEEP); deep["date"] = pd.to_datetime(deep["date"])
def ysym(sym):
    d = deep[deep["symbol"] == sym].drop_duplicates("date", keep="last").set_index("date").sort_index()
    return d[["open", "high", "low", "close", "volume"]].astype(float)
bd = pd.read_csv(os.path.join(ROOT, "data_store/market_data/binance_daily.csv")); bd["date"] = pd.to_datetime(bd["date"])
def bsym(sym):
    d = bd[bd["symbol"] == sym].drop_duplicates("date", keep="last").set_index("date").sort_index()
    return d[["open", "high", "low", "close", "volume"]].astype(float)
dv = pd.read_csv(os.path.join(ROOT, "data_store/market_data/crypto_dvol.csv")); dv["date"] = pd.to_datetime(dv["date"])
def dvol(c):
    return dv[dv["currency"] == c].drop_duplicates("date", keep="last").set_index("date")["close"].astype(float)
VIX = ysym("^VIX")["close"]
MKT = {"NIFTY": (ysym("^NSEI"), ysym("^INDIAVIX")["close"], 252), "BANKNIFTY": (ysym("^NSEBANK"), ysym("^INDIAVIX")["close"], 252),
       "BTC": (ysym("BTC-USD"), dvol("BTC"), 365), "ETH": (bsym("ETHUSDT"), dvol("ETH"), 365), "SOL": (bsym("SOLUSDT"), dvol("BTC"), 365),
       "GOLD": (ysym("GC=F"), VIX, 252), "US": (ysym("^GSPC"), VIX, 252)}

def pct(x, w):
    return x.rolling(w, min_periods=w // 2).apply(lambda a: (a[:-1] < a[-1]).mean() * 100 if len(a) > 1 else np.nan, raw=True)

def states(m):
    b, iv, ann = MKT[m]
    h, l, c = b["high"], b["low"], b["close"]
    up, dn = h.diff(), -l.diff()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdi = 100 * up.where((up > dn) & (up > 0), 0).ewm(alpha=1 / 14, adjust=False).mean() / atr
    ndi = 100 * dn.where((dn > up) & (dn > 0), 0).ewm(alpha=1 / 14, adjust=False).mean() / atr
    adx = (100 * (pdi - ndi).abs() / (pdi + ndi)).ewm(alpha=1 / 14, adjust=False).mean()
    rv = np.log(c).diff().rolling(20).std()
    rvp = pct(rv, ann)
    S = pd.DataFrame(index=b.index)
    S["trend"] = np.where(adx >= 25, "Trending", np.where(adx < 18, "Sideways", "Mixed"))
    S["direction"] = np.where(c > c.rolling(200).mean(), "above 200d", "below 200d")
    S["vol"] = np.where(rvp < 33, "Low", np.where(rvp > 67, "High", "Normal"))
    ivr = iv.reindex(b.index).ffill(limit=5)
    ivp = pct(ivr, 252)
    S["ivol"] = np.where(ivp.isna(), None, np.where(ivp < 33, "Low", np.where(ivp > 67, "High", "Normal")))
    ch = ivr / ivr.shift(5) - 1
    S["ivchg"] = np.where(ch.isna(), None, np.where(ch > 0.10, "Rising", np.where(ch < -0.10, "Falling", "Flat")))
    vx = VIX.reindex(b.index).ffill(limit=5)
    S["shock"] = np.where((vx / vx.shift(1) - 1) > 0.15, "VIX shock day", "normal")
    try:
        f = XR.features(b, ivr if m in ("NIFTY", "BANKNIFTY") else None, ann)
        S["regime"] = XR.label(f).reindex(b.index)
    except Exception:
        S["regime"] = None
    S.loc[S.index[:200], ["direction"]] = None
    return S

# ---------- trades ----------
def load_trades():
    rows = []
    for f in glob.glob(f"{MB}/results/*/trades_*.csv"):
        strat = f.split("/results/")[1].split("/")[0]
        name = os.path.basename(f)[7:-4]
        mk = re.match(r"(NIFTY|BANKNIFTY|BTC|ETH|SOL|GOLD)(.*)", name)
        if not mk:
            continue
        try:
            d = pd.read_csv(f)
        except Exception:
            continue
        ent = next((c for c in ("entry_time", "entry_date", "entry") if c in d), None)
        pnl = next((c for c in ("net_pnl", "pnl") if c in d), None)
        if not (ent and pnl) or len(d) < 10:
            continue
        t = pd.DataFrame({"entry": pd.to_datetime(d[ent], errors="coerce", utc=True).dt.tz_localize(None), "pnl": pd.to_numeric(d[pnl], errors="coerce"),
                          "side": d["side"].astype(str).str.lower() if "side" in d else "na"}).dropna(subset=["entry", "pnl"])
        t["market"], t["strategy"], t["leg"] = mk.group(1), strat, f"{strat} · {name}"
        rows.append(t)
    for f in glob.glob(f"{ROOT}/data_store/master_backtester/options_trades/trades_*.csv"):
        name = os.path.basename(f)[7:-4]; m = name.split("_")[0]; strat = name[len(m) + 1:]
        d = pd.read_csv(f)
        t = pd.DataFrame({"entry": pd.to_datetime(d["entry"], errors="coerce"), "pnl": pd.to_numeric(d["pnl"], errors="coerce"), "side": "short_vol"}).dropna()
        t["market"], t["strategy"], t["leg"] = m, strat, f"{strat} · {m} options"
        rows.append(t)
    for f in glob.glob(f"{MB}/experiments/us_stocks/results/book_trades_*.csv"):
        strat = os.path.basename(f)[12:-4]
        d = pd.read_csv(f)
        t = pd.DataFrame({"entry": pd.to_datetime(d["entry"], errors="coerce"), "pnl": pd.to_numeric(d["r"], errors="coerce"), "side": "long"}).dropna()
        t["market"], t["strategy"], t["leg"] = "US", strat, f"{strat} · US stocks book"
        rows.append(t)
    T = pd.concat(rows, ignore_index=True).drop_duplicates(["leg", "entry", "pnl"])
    sd = T.groupby("leg")["pnl"].transform("std")
    T["u"] = T["pnl"] / sd
    T["family"] = T["strategy"].map(family)
    T.loc[T["side"] == "short_vol", "family"] = "short_vol"
    T["part"] = np.where(T["entry"] < SPLIT, "train", "test")
    return T[T["u"].notna() & np.isfinite(T["u"])]

def tag(T):
    parts = []
    for m, g in T.groupby("market"):
        S = states(m)
        S.index = S.index + pd.Timedelta(days=1)            # state known from the next day on
        k = g["entry"].dt.normalize()
        st = S.reindex(S.index.union(k.unique())).sort_index().ffill(limit=5).reindex(k.values)
        st.index = g.index
        parts.append(pd.concat([g, st], axis=1))
    T = pd.concat(parts)
    T["weekday"] = T["entry"].dt.day_name().str[:3]
    nd = T["entry"].dt.normalize()
    T["expiry"] = np.where(T["market"].isin(["NIFTY", "BANKNIFTY"]),
                           np.where(((nd < "2025-09-01") & (nd.dt.dayofweek == 3)) | ((nd >= "2025-09-01") & (nd.dt.dayofweek == 1)), "expiry day", "other day"), None)
    dom = nd.dt.day; dim = nd.dt.days_in_month
    T["month_edge"] = np.where((dom <= 3) | (dom >= dim - 2), "month edge", "mid month")
    T["with_200d"] = np.where(T["side"].isin(["long", "short"]) & T["direction"].notna(),
                              np.where((T["side"] == "long") == (T["direction"] == "above 200d"), "with 200d", "against 200d"), None)
    return T

def tstat(x):
    x = x.dropna(); return x.mean() / (x.std() / np.sqrt(len(x))) if len(x) > 2 and x.std() > 0 else np.nan

DIMS = ["trend", "vol", "ivol", "ivchg", "shock", "regime", "with_200d", "weekday", "expiry", "month_edge"]

def cells(T, by):
    r = []
    for keys, g in T.groupby(by + ["part"]):
        keys = keys if isinstance(keys, tuple) else (keys,)
        r.append(dict(zip(by + ["part"], keys), n=len(g), avg_u=round(g["u"].mean(), 3), win=round(100 * (g["u"] > 0).mean(), 1), t=round(tstat(g["u"]), 2)))
    return pd.DataFrame(r)

def main():
    T = tag(load_trades())
    T.to_csv(f"{OUT}/trades_tagged.csv.gz", index=False, compression="gzip")
    info = {"trades": len(T), "legs": T["leg"].nunique(), "strategies": T["strategy"].nunique(),
            "from": str(T["entry"].min())[:10], "to": str(T["entry"].max())[:10], "train": int((T.part == "train").sum()), "test": int((T.part == "test").sum()),
            "by_family": T["family"].value_counts().to_dict(), "by_market": T["market"].value_counts().to_dict()}
    # 1) family x condition
    fam = pd.concat([cells(T, ["family", d]).rename(columns={d: "state"}).assign(dim=d) for d in DIMS])
    fam.to_csv(f"{OUT}/family_by_condition.csv", index=False)
    # 2) hypotheses
    H = []
    def comp(mask_fam, dim, a, b, name):
        for part in ("train", "test"):
            g = T[mask_fam & (T.part == part)]
            x, y = g[g[dim] == a]["u"], g[g[dim] == b]["u"]
            diff = x.mean() - y.mean()
            se = np.sqrt(x.var() / max(1, len(x)) + y.var() / max(1, len(y)))
            H.append({"hypothesis": name, "part": part, "a": a, "n_a": len(x), "avg_a": round(x.mean(), 3), "b": b, "n_b": len(y), "avg_b": round(y.mean(), 3),
                      "diff": round(diff, 3), "t": round(diff / se, 2) if se > 0 else None})
    comp(T.family == "trend", "trend", "Trending", "Sideways", "H1 trend-following: Trending beats Sideways")
    comp(T.family == "meanrev", "trend", "Sideways", "Trending", "H2 mean-reversion: Sideways beats Trending")
    comp(T.family == "short_vol", "ivchg", "Falling", "Rising", "H3 short-vol options: implied vol Falling beats Rising")
    comp(T.family.isin(["trend", "meanrev", "other"]), "with_200d", "with 200d", "against 200d", "H4 directional: with the 200-day beats against")
    comp(T.family.isin(["trend", "meanrev", "other"]), "vol", "Low", "High", "H5 directional: Low vol beats High vol (per trade, own-spread units)")
    comp(T.index == T.index, "shock", "normal", "VIX shock day", "H6 all: entries on normal days beat entries after a VIX shock")
    HD = pd.DataFrame(H); HD.to_csv(f"{OUT}/hypotheses.csv", index=False)
    # 3) per leg (registry legs first): best / worst conditions that hold in BOTH periods
    reg = json.load(open(f"{MB}/engine/leg_registry.json"))
    regset = {(l["strategy"], l["market"].split(" ")[0].upper()) for l in reg}
    L = []
    for leg, g in T.groupby("leg"):
        if len(g) < 40:
            continue
        s, m = g["strategy"].iloc[0], g["market"].iloc[0]
        base_tr, base_te = g[g.part == "train"]["u"].mean(), g[g.part == "test"]["u"].mean()
        for d in DIMS:
            for st, h in g.groupby(d):
                tr, te = h[h.part == "train"]["u"], h[h.part == "test"]["u"]
                if len(tr) < 12 or len(te) < 8:
                    continue
                L.append({"leg": leg, "strategy": s, "market": m, "family": g["family"].iloc[0], "registry": (s, m) in regset, "dim": d, "state": st,
                          "n_train": len(tr), "avg_train": round(tr.mean(), 3), "base_train": round(base_tr, 3),
                          "n_test": len(te), "avg_test": round(te.mean(), 3), "base_test": round(base_te, 3)})
    LD = pd.DataFrame(L)
    LD["edge_train"], LD["edge_test"] = LD["avg_train"] - LD["base_train"], LD["avg_test"] - LD["base_test"]
    LD["holds"] = np.where((LD.edge_train > 0.15) & (LD.edge_test > 0.10), "better here", np.where((LD.edge_train < -0.15) & (LD.edge_test < -0.10), "worse here", ""))
    LD.to_csv(f"{OUT}/leg_by_condition.csv", index=False)
    # 4) overlay test: skip family x (trend, vol) cells that lost money on TRAIN
    tr = T[T.part == "train"]; te = T[T.part == "test"].copy()
    bad = tr.groupby(["family", "trend", "vol"])["u"].agg(["mean", "count"])
    bad = bad[(bad["mean"] < 0) & (bad["count"] >= 40)].index
    key = list(zip(te.family, te.trend, te.vol))
    skip = np.array([k in set(bad) for k in key])
    base = te["u"].sum(); kept = te.loc[~skip, "u"].sum()
    sh = lambda x: x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 else np.nan
    lucky = []
    for _ in range(300):
        perm = RNG.permutation(len(te))
        lucky.append(te["u"].values[~skip[perm]].sum())
    ov = {"rule": "skip a family's trades in (trend x vol) cells that lost money on train (n>=40)", "bad_cells": [list(b) for b in bad],
          "test_trades": len(te), "skipped": int(skip.sum()), "test_base_sum_u": round(base, 1), "test_kept_sum_u": round(kept, 1),
          "test_base_avg_u": round(te["u"].mean(), 4), "test_kept_avg_u": round(te.loc[~skip, "u"].mean(), 4),
          "test_skipped_avg_u": round(te.loc[skip, "u"].mean(), 4) if skip.any() else None,
          "luck_95pct_kept_sum": round(float(np.percentile(lucky, 95)), 1), "beats_luck": bool(kept > np.percentile(lucky, 95))}
    json.dump({"info": info, "overlay": ov}, open(f"{OUT}/summary.json", "w"), indent=1, default=str)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 400); pd.set_option("display.max_colwidth", 60)
    print(json.dumps(info, default=str)); print(HD.to_string(index=False)); print(json.dumps(ov, default=str))
    print(LD[LD.holds != ""].sort_values(["registry", "leg"], ascending=[False, True]).to_string(index=False)[:20000])

if __name__ == "__main__":
    main()
