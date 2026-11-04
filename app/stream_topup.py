"""Used by stream_hotfix.py on a stream started before top_up_missing existed (same logic as stream.top_up_missing)."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd

_PREV: dict = {}


def chart_quote(U, sym):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _PREV.get(sym, (None,))[0] != day:
        d, tz = U.yahoo_chart(sym, "1d", "5d")
        dates = d["ts"].dt.tz_convert(tz).dt.date
        prev = d[dates < pd.Timestamp.now(tz=tz).date()] if len(d) > 1 else d
        _PREV[sym] = (day, float(prev["close"].iloc[-1]) if len(prev) else None)
    df, _ = U.yahoo_chart(sym, "1m", "5d")
    if not len(df):
        return None
    last, pc = float(df["close"].iloc[-1]), _PREV[sym][1]
    return {"last": last, "prev_close": pc, "t": df["ts"].iloc[-1].timestamp(),
            "chg_pct": None if not pc else round((last / pc - 1) * 100, 2), "seen": time.time()}


def top_up(S):
    st = S.FEEDS.setdefault("_topup", {"runs": 0})
    st["runs"] = st.get("runs", 0) + 1
    st["have"] = sorted(k for k in S.QUOTES if k.startswith("^NS"))
    for sym in ("^NSEI", "^NSEBANK"):
        q = S.QUOTES.get(sym)
        if q and time.time() - q.get("seen", 0) < 60:
            continue
        try:
            r = chart_quote(S.U, sym)
            if r:
                S.QUOTES[sym] = r
        except Exception as e:
            st[sym] = str(e)[:120]
