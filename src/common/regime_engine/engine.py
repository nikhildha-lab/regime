"""
Multi-timeframe NIFTY regime engine (BRD §6-§7), v1.

Profiles in v1 (what the offline data supports):
  - "daily": positional horizon, daily bars, groups TREND + VOLATILITY + BREADTH
  - "4h":    swing horizon, 4H bars, groups TREND + VOLATILITY + BREADTH (breadth = previous day, lag 1)
15m / 1h profiles are wired the same way once intraday candles are available (Phase 0).

Output of call(as_of):
  {as_of, weight_set_version, profiles: {daily: {...}, 4h: {...}}, combined: {...}, calendar: {...}}
Each profile block carries two layers:
  1. DESCRIPTIVE state (used for strategy selection): trend_state (Uptrend/Sideways/Downtrend),
     vol_state (Low/Normal/High) and regime_name (one of the 6 plan regimes). No fitted weights.
  2. FORECAST call (BRD §6.3): regime_label (Bullish/Bearish/Neutral), confidence_tier, meta_score,
     top_factors, data_completeness - from the versioned weight set. Informational until it
     passes the BRD §11 go/no-go bar.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import numpy as np
import pandas as pd

from . import data as D
from .aggregator import group_scores, meta_aggregate
from .contract import FeatureScore, Provenance, clip_score
from .features import price_features as PF
from .features import breadth as BR
from .features import market_features as MF
from .features import indicator_panel as IP
from .features.calendar_tags import calendar_tags, event_tags

WEIGHTS_DIR = os.path.join(os.path.dirname(__file__), "weights")
ALL_FEATURES = {**PF.FEATURES, **BR.FEATURES, **MF.FEATURES, **IP.FEATURES}
FEATURE_GROUPS = {fid: g for fid, (g, _) in ALL_FEATURES.items()}
SIGN_CONVENTION = {fid: s for fid, (_, s) in ALL_FEATURES.items()}
GROUPS = ["TREND", "VOLATILITY", "BREADTH", "POSITIONING", "GLOBAL", "TECHNICALS"]
PROFILES = ("daily", "4h", "1h", "30m", "15m", "5m")
FIT_PROFILES = ("daily", "4h", "1h")          # 30m/15m/5m have ~60 days: descriptive + panel only
MTF_ORDER = ["5m", "15m", "30m", "1h", "4h", "daily"]   # lower -> higher
TIER_ORDER = ["Low", "Medium", "High"]


def latest_weights_path(source: str = "futures") -> str:
    files = sorted(f for f in os.listdir(WEIGHTS_DIR) if f.startswith("weights_") and f.endswith(".json")
                   and (("_futures_" in f) == (source == "futures")))
    if not files:
        raise FileNotFoundError("no weight set in regime_engine/weights - run scripts/regime_engine_backfill.py --fit")
    return os.path.join(WEIGHTS_DIR, files[-1])


STATE_STRONG, STATE_TREND = 0.5, 0.2      # a-priori thresholds on the TREND group score (not fitted)


def describe_state(trend_score, vol):
    """Descriptive regime (what the market IS doing), used by the strategy scorecard and recommender.
    Built from the TREND group score + realized-vol state only; no fitted weights involved."""
    if trend_score is None or (isinstance(trend_score, float) and np.isnan(trend_score)):
        return None, None
    t = float(trend_score)
    direction = "Uptrend" if t > STATE_TREND else "Downtrend" if t < -STATE_TREND else "Sideways"
    if direction == "Uptrend":
        name = "Strong uptrend" if t > STATE_STRONG and vol != "High" else "Grinding uptrend"
    elif direction == "Downtrend":
        name = "Panic" if t < -STATE_STRONG and vol == "High" else "Downtrend"
    else:
        name = "Choppy range" if vol == "High" else "Calm range"
    return direction, name


def _source_for(fid: str, price_source: str) -> str:
    if fid in PF.FEATURES:
        return price_source
    if fid in BR.FEATURES:
        return f"csv:{D.STOCKS_CSV}"
    if fid.startswith(("C.2",)):
        return f"csv:{D.INDEX_FUTURES}"
    if fid.startswith(("C.4", "C.3p")):
        return f"csv:{D.PARTICIPANT_OI}"
    return f"csv:{D.YAHOO_DAILY}"


class RegimeEngine:
    def __init__(self, weights_path: Optional[str] = None, price_source: str = D.DEFAULT_SOURCE):
        """price_source: "futures" (NIFTY near-month futures, default) or "spot" (NIFTY 50 index)."""
        self._frames: dict = {}
        self.weights = None
        self.price_source = price_source
        if weights_path or os.path.isdir(WEIGHTS_DIR):
            try:
                with open(weights_path or latest_weights_path(price_source)) as f:
                    self.weights = json.load(f)
            except FileNotFoundError:
                self.weights = None

    # ---------- feature + group frame (no weights needed) ----------
    def _daily_context(self) -> pd.DataFrame:
        """Breadth + Phase 0 market features on India trading dates, as known at that day's 15:30 close."""
        if "_ctx" in self._frames:
            return self._frames["_ctx"]
        px = D.load_nifty_daily_long()
        dates = pd.DatetimeIndex(px.index)
        br = BR.compute_breadth_features(D.load_stock_panel())
        ctx = br[list(BR.FEATURES)].reindex(dates)
        mf = MF.compute_market_features(dates, px["close"])
        ctx = pd.concat([ctx, mf], axis=1)
        self._frames["_ctx"] = ctx
        return ctx

    def _load_px(self, profile: str) -> pd.DataFrame:
        if self.price_source == "futures":
            return D.load_futures_bars(profile, "NIFTY")
        return D.load_bars(profile)

    def feature_frame(self, profile: str) -> pd.DataFrame:
        if profile in self._frames:
            return self._frames[profile]
        px = self._load_px(profile)
        feats = PF.compute_price_features(px["close"])
        feats["vol_state"] = PF.vol_state(px["close"])
        panel = IP.compute_panel(px, daily=(profile == "daily"))
        for c in panel.columns:
            feats[c] = panel[c]
        ctx = self._daily_context()
        ctx_cols = list(ctx.columns)
        if profile == "daily":
            b = ctx.reindex(px.index)                                     # known at the same 15:30 close
        else:
            # intraday bars only see daily context from a date STRICTLY before the bar's day
            day = pd.DataFrame({"day": px.index.tz_localize(None).normalize()})
            right = ctx.reset_index()
            right = right.rename(columns={right.columns[0]: "cdate"}).sort_values("cdate")
            m = pd.merge_asof(day, right, left_on="day", right_on="cdate", allow_exact_matches=False,
                              tolerance=pd.Timedelta(days=7))
            b = m.set_index(px.index)
        for c in ctx_cols:
            feats[c] = b[c].values
        for g in GROUPS:
            cols = [f for f, gg in FEATURE_GROUPS.items() if gg == g]
            feats[f"group_{g}"] = feats[cols].mean(axis=1, skipna=True)
        feats["close"] = px["close"]
        feats["available_at"] = px["available_at"]
        feats.attrs["source"] = px.attrs.get("source")
        self._frames[profile] = feats
        return feats

    # ---------- one profile, one bar ----------
    def score_bar(self, profile: str, ts, weights: Optional[dict] = None) -> dict:
        fr = self.feature_frame(profile)
        row = fr.loc[ts]
        w = weights or (self.weights or {}).get("profiles", {}).get(profile)
        feature_scores = {}
        objs = []
        for fid in FEATURE_GROUPS:
            s = clip_score(row[fid])
            feature_scores[fid] = s
            objs.append(FeatureScore(
                feature_id=fid, group=FEATURE_GROUPS[fid], score=s,
                sign_convention=SIGN_CONVENTION[fid],
                provenance=Provenance(source=_source_for(fid, fr.attrs.get("source")),
                                      instrument_or_series_id=f"NIFTY_{profile.upper()}" if fid in PF.FEATURES else fid.split("_")[0],
                                      as_of=str(row["available_at"]), raw_value=None)))
        groups = group_scores(feature_scores, FEATURE_GROUPS)
        if w is not None:
            out = meta_aggregate(groups, w["groups"], w["label_threshold"], w["confidence"]["high"],
                                 w["confidence"]["medium"], feature_scores, FEATURE_GROUPS)
        else:   # short-history timeframe: no fitted weights -> no forecast, descriptive + panel only
            out = {"meta_score": None, "regime_label": None, "confidence_tier": None, "top_factors": [],
                   "data_completeness": None, "forecast_note": "no fitted weights for this timeframe"}
        vol = row["vol_state"] if isinstance(row["vol_state"], str) else None
        out.update({
            "profile": profile, "bar": str(ts), "available_at": str(row["available_at"]),
            "close": float(row["close"]), "vol_state": vol,
            "trend_state": describe_state(groups.get("TREND"), vol)[0],
            "regime_name": describe_state(groups.get("TREND"), vol)[1],
            "group_scores": {g: (None if v is None else round(v, 4)) for g, v in groups.items()},
            "panel": IP.panel_row(row),
            "features": [o.to_dict() for o in objs],
        })
        return out

    def last_bar(self, profile: str, as_of) -> Optional[pd.Timestamp]:
        fr = self.feature_frame(profile)
        as_of = pd.Timestamp(as_of)
        if as_of.tzinfo is None:
            as_of = as_of.tz_localize(D.IST)
        ok = fr.index[fr["available_at"] <= as_of]
        return ok[-1] if len(ok) else None

    # ---------- multi-timeframe call ----------
    def call(self, as_of) -> dict:
        if self.weights is None:
            raise RuntimeError("no fitted weight set loaded (BRD §6.5: strategies may not consume unfit weights)")
        profiles = {}
        for p in PROFILES:
            ts = self.last_bar(p, as_of)
            profiles[p] = self.score_bar(p, ts) if ts is not None and self._fresh(p, ts, as_of) else None
        combined = self._combine(profiles)
        return {
            "as_of": str(pd.Timestamp(as_of)),
            "weight_set_version": self.weights["version"],
            "price_source": "NIFTY near-month futures" if self.price_source == "futures" else "NIFTY 50 index",
            "weight_set_status": self.weights["status"],
            "profiles": profiles,
            "combined": combined,
            "mtf_table": self.mtf_table(profiles),
            "calendar": {**calendar_tags(pd.Timestamp(as_of)), **event_tags(pd.Timestamp(as_of))},
            "india_vix": self.india_vix(as_of),
        }

    def india_vix(self, as_of) -> dict:
        """Latest India VIX known at as_of: 1h bar if intraday data covers it, else the last daily close."""
        as_of = pd.Timestamp(as_of)
        as_of = as_of if as_of.tzinfo else as_of.tz_localize(D.IST)
        out = {"value": None, "as_of_bar": None, "source": None}
        try:
            h = D.load_bars("1h", "^INDIAVIX")
            ok = h[h["available_at"] <= as_of]
            if len(ok) and (as_of - ok.index[-1]) <= pd.Timedelta(days=4):
                out = {"value": round(float(ok["close"].iloc[-1]), 2), "as_of_bar": str(ok.index[-1]), "source": "^INDIAVIX 1h"}
        except Exception:
            pass
        if out["value"] is None:
            d = D.load_bars("daily", "^INDIAVIX")
            ok = d[d["available_at"] <= as_of]
            if len(ok):
                out = {"value": round(float(ok["close"].iloc[-1]), 2), "as_of_bar": str(ok.index[-1].date()), "source": "^INDIAVIX daily close"}
        v = out["value"]
        out["bucket"] = None if v is None else ("VIX<12" if v < 12 else "12-14.5" if v < 14.5 else "VIX>=14.5")
        return out

    @staticmethod
    def _fresh(profile: str, ts, as_of) -> bool:
        """Do not show an intraday timeframe whose last bar is more than 5 days old (data gap)."""
        if profile == "daily":
            return True
        as_of = pd.Timestamp(as_of)
        as_of = as_of if as_of.tzinfo else as_of.tz_localize(D.IST)
        return (as_of - ts) <= pd.Timedelta(days=5)

    @staticmethod
    def mtf_table(profiles: dict) -> dict:
        """The 'TF | Trend | Wait Zone' table: panel trend per timeframe + wait-zone rule + summary."""
        rows, avail = [], [tf for tf in MTF_ORDER if profiles.get(tf)]
        for i, tf in enumerate(avail):
            p = profiles[tf]["panel"]
            higher = profiles[avail[i + 1]]["panel"]["trend"] if i + 1 < len(avail) else None
            ich = profiles[tf]["features"]
            ich = next((f["score"] for f in ich if f["feature_id"] == "T.4_ichimoku"), None)
            wz = IP.wait_zone(p["trend"], p["confluence_up"], p["confluence_down"], ich, higher)
            rows.append({"tf": tf, "trend": p["trend"], "wait_zone": "Active" if wz else "Inactive",
                         "confluence": f"up {p['confluence_up']} / down {p['confluence_down']}",
                         "bar": profiles[tf]["bar"]})
        ups = sum(r["trend"] == "Up" for r in rows)
        downs = sum(r["trend"] == "Down" for r in rows)
        active = sum(r["wait_zone"] == "Active" for r in rows)
        summary = {"trend": "Up" if ups > downs else "Down" if downs > ups else "Flat",
                   "wait": "ON" if rows and active * 2 >= len(rows) else "OFF",
                   "votes": f"up {ups} / down {downs} of {len(rows)} timeframes",
                   "rule": "majority of timeframes; wait ON when at least half are in their wait zone"}
        return {"rows": rows, "summary": summary,
                "wait_zone_rule": "Active if |up-down| <= 1, price inside the Ichimoku cloud, or trend disagrees with the next higher timeframe (assumed rule)"}

    @staticmethod
    def _combine(profiles: dict) -> dict:
        d, h = profiles.get("daily"), profiles.get("4h")
        h1 = profiles.get("1h")
        if not d or d["regime_name"] is None:
            return {"regime_name": None, "trend_state": None, "timeframes_agree": None, "state_confidence": None}
        others = [x for x in (h, h1) if x is not None and x.get("trend_state")]
        n_agree = sum(1 for x in others if x["trend_state"] == d["trend_state"])
        agree = bool(others) and n_agree == len(others)
        conf = "High" if agree else "Medium" if n_agree >= 1 else "Low"
        return {
            "regime_name": d["regime_name"], "trend_state": d["trend_state"], "vol_state": d["vol_state"],
            "timeframes_agree": agree, "state_confidence": conf,
            "swing_4h_state": None if h is None else h["regime_name"],
            "intraday_1h_state": None if h1 is None else h1["regime_name"],
            "forecast_label": d["regime_label"], "forecast_confidence": d["confidence_tier"],
            "forecast_note": "forecast label is informational only: the fitted weights have NOT passed the BRD §11 go/no-go bar",
            "rule": "daily state sets the regime; High = 4H and 1H agree, Medium = one agrees, Low = none",
        }
