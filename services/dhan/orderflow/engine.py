"""
Volume profile + order flow engine.

Works on 1-minute OHLCV bars. If a bar has real buy/sell volume (from the tick
recorder) it uses it; otherwise it ESTIMATES delta from where the bar closed:
    buy volume  = volume x (close - low) / (high - low)
    sell volume = volume - buy volume
    delta       = buy - sell
This estimate is rough. Real ticks are better - that's what recorder.py is for.
"""

import numpy as np
import pandas as pd

import config as C


# ---------------- loading ----------------
def load_bars(path):
    df = pd.read_csv(path, parse_dates=["ts"])
    t = df["ts"].dt.strftime("%H:%M")
    df = df[(t >= C.MARKET_OPEN) & (t < "15:30")].copy()
    df["date"] = df["ts"].dt.date
    return df.sort_values("ts").reset_index(drop=True)


# ---------------- delta ----------------
def add_delta(df):
    if {"buy_vol", "sell_vol"}.issubset(df.columns):
        df["delta"] = df["buy_vol"] - df["sell_vol"]
        return df
    rng = (df["high"] - df["low"]).to_numpy()
    pos = np.where(rng > 0, (df["close"] - df["low"]) / np.where(rng > 0, rng, 1), np.nan)
    # flat bars: use tick rule (close vs previous close)
    tick = np.sign(df["close"].diff().fillna(0)).to_numpy()
    pos = np.where(np.isnan(pos), (tick + 1) / 2, pos)
    buy = df["volume"] * pos
    df["delta"] = 2 * buy - df["volume"]
    return df


# ---------------- volume profile ----------------
def profile(bars, bin_size):
    """Return (POC, VAH, VAL) for a set of bars. Volume spread evenly across each bar's range."""
    lo = np.floor(bars["low"].min() / bin_size)
    hi = np.ceil(bars["high"].max() / bin_size)
    n = int(hi - lo) + 1
    vol = np.zeros(n)
    for l, h, v in zip(bars["low"].to_numpy(), bars["high"].to_numpy(), bars["volume"].to_numpy()):
        a, b = int(np.floor(l / bin_size) - lo), int(np.floor(h / bin_size) - lo)
        vol[a:b + 1] += v / (b - a + 1)
    poc = int(vol.argmax())
    total, inside = vol.sum(), vol[poc]
    up, dn = poc, poc
    while inside < C.VALUE_AREA_PCT * total and (up < n - 1 or dn > 0):
        nxt_up = vol[up + 1] if up < n - 1 else -1
        nxt_dn = vol[dn - 1] if dn > 0 else -1
        if nxt_up >= nxt_dn:
            up += 1
            inside += nxt_up
        else:
            dn -= 1
            inside += nxt_dn
    px = lambda i: (lo + i + 0.5) * bin_size
    return px(poc), px(up) + bin_size / 2, px(dn) - bin_size / 2


def daily_levels(df):
    """One row per day: yesterday's POC/VAH/VAL/high/low + today's open and IB."""
    rows = []
    prev = None
    for d, g in df.groupby("date"):
        bin_size = g["close"].iloc[0] * C.PROFILE_BIN_PCT
        poc, vah, val = profile(g, bin_size)
        ib = g[g["ts"].dt.strftime("%H:%M") < C.IB_END]
        row = {"date": d, "open": g["open"].iloc[0],
               "ib_high": ib["high"].max(), "ib_low": ib["low"].min()}
        if prev is not None:
            row.update(prev)
            rows.append(row)
        prev = {"p_poc": poc, "p_vah": vah, "p_val": val,
                "p_high": g["high"].max(), "p_low": g["low"].min(), "p_close": g["close"].iloc[-1]}
    return pd.DataFrame(rows).set_index("date")


# ---------------- 5-minute signal bars ----------------
def signal_bars(df):
    g = df.set_index("ts").groupby("date")
    out = []
    for d, day in g:
        b = day.resample(f"{C.SIGNAL_BAR_MIN}min", origin="start_day", offset="15min").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last",
             "volume": "sum", "delta": "sum"}).dropna(subset=["open"])
        b["cvd"] = b["delta"].cumsum()
        b["date"] = d
        out.append(b)
    b = pd.concat(out)
    b["ready"] = b.index + pd.Timedelta(minutes=C.SIGNAL_BAR_MIN)  # when the bar is complete
    b["avg_vol"] = b["volume"].rolling(20, min_periods=5).mean().shift(1)
    b["avg_rng"] = (b["high"] - b["low"]).rolling(20, min_periods=5).mean().shift(1)
    return b.reset_index()
