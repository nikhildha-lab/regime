"""
HMM challenger: a 3-state hidden Markov model on [daily log return, log 20-day realised vol].

Discipline (no look-ahead):
  * refit once a year (every January) on data BEFORE that year only; expanding window for markets with long history,
    rolling 3 years for crypto; at least 500 days of training data
  * inputs standardised with TRAINING mean/std only; returns clipped at +-5 sd (crude fat-tail guard: hmmlearn has
    no Student-t emissions)
  * states decoded with the FORWARD filter only (probability of each state given data up to today) - Viterbi or
    forward-backward would use later days of the year
  * label switching: states re-sorted after every fit by mean return / return std of the state (best = 0)
Exposure = sum over states of P(state) x {best 1.0, middle 0.6, worst 0.15}.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import logsumexp

EXP_BY_RANK = np.array([1.0, 0.6, 0.15])


def _inputs(close: pd.Series) -> pd.DataFrame:
    lr = np.log(close).diff()
    v = np.log(lr.rolling(20).std())
    return pd.DataFrame({"r": lr, "v": v}).dropna()


def _fit(X: np.ndarray, seed: int):
    from hmmlearn.hmm import GaussianHMM
    best, best_ll = None, -np.inf
    for k in range(5):
        m = GaussianHMM(n_components=3, covariance_type="full", n_iter=200, random_state=seed + k, tol=1e-4)
        try:
            m.fit(X)
            ll = m.score(X)
        except Exception:
            continue
        if ll > best_ll and np.all(np.isfinite(m.transmat_)):
            best, best_ll = m, ll
    return best


def _forward_filter(m, X: np.ndarray) -> np.ndarray:
    """P(state_t | x_1..x_t) for every t (causal)."""
    logB = m._compute_log_likelihood(X)
    logA = np.log(np.clip(m.transmat_, 1e-12, None))
    la = np.log(np.clip(m.startprob_, 1e-12, None)) + logB[0]
    out = np.empty_like(logB)
    out[0] = la - logsumexp(la)
    for t in range(1, len(X)):
        la = logsumexp(out[t - 1][:, None] + logA, axis=0) + logB[t]
        out[t] = la - logsumexp(la)
    return np.exp(out)


def walk_forward(close: pd.Series, start_year: int = 2011, rolling_years: int | None = None, seed: int = 7) -> pd.DataFrame:
    """Returns per day: P(best), P(middle), P(worst), state (argmax rank), exposure. NaN before the first fit."""
    X = _inputs(close)
    out = pd.DataFrame(index=X.index, columns=["p_best", "p_mid", "p_worst", "state", "exposure"], dtype=float)
    last = X.index[-1].year
    for y in range(start_year, last + 1):
        tr = X[X.index < f"{y}-01-01"]
        if rolling_years:
            tr = tr[tr.index >= f"{y - rolling_years}-01-01"]
        if len(tr) < 500:
            continue
        mu, sd = tr.mean(), tr.std()
        Z = ((tr - mu) / sd).clip(-5, 5).values
        m = _fit(Z, seed + y)
        if m is None:
            continue
        # sort states: mean return / return std (column 0 = returns)
        means = m.means_[:, 0]
        stds = np.sqrt(np.array([c[0, 0] for c in m.covars_]))
        order = np.argsort(-(means / stds))
        # filter from the start of the training window through the end of year y (state carries over continuously)
        seg = X[(X.index >= tr.index[0]) & (X.index < f"{y + 1}-01-01")]
        P = _forward_filter(m, ((seg - mu) / sd).clip(-5, 5).values)[:, order]
        yr = seg.index >= f"{y}-01-01"
        P = P[yr]
        idx = seg.index[yr]
        out.loc[idx, ["p_best", "p_mid", "p_worst"]] = P
        out.loc[idx, "state"] = P.argmax(axis=1)
        out.loc[idx, "exposure"] = P @ EXP_BY_RANK
    return out


# ---------------------------------------------------------------------------------------------------------
# GMM challenger: Gaussian mixture (3 clusters) on the same two inputs. No memory of yesterday's state
# (a pure "which cluster does today look like"), so it is causal by construction. Same yearly refit,
# same standardisation on training data only, same sorting of clusters by return / risk.
# ---------------------------------------------------------------------------------------------------------
def _fit_gmm(X: np.ndarray, seed: int):
    from sklearn.mixture import GaussianMixture
    return GaussianMixture(n_components=3, covariance_type="full", n_init=5, random_state=seed).fit(X)


def gmm_walk_forward(close: pd.Series, start_year: int = 2011, rolling_years: int | None = None, seed: int = 7) -> pd.DataFrame:
    X = _inputs(close)
    out = pd.DataFrame(index=X.index, columns=["p_best", "p_mid", "p_worst", "state", "exposure"], dtype=float)
    for y in range(start_year, X.index[-1].year + 1):
        tr = X[X.index < f"{y}-01-01"]
        if rolling_years:
            tr = tr[tr.index >= f"{y - rolling_years}-01-01"]
        if len(tr) < 500:
            continue
        mu, sd = tr.mean(), tr.std()
        m = _fit_gmm(((tr - mu) / sd).clip(-5, 5).values, seed + y)
        stds = np.sqrt(m.covariances_[:, 0, 0])
        order = np.argsort(-(m.means_[:, 0] / stds))
        te = X[(X.index >= f"{y}-01-01") & (X.index < f"{y + 1}-01-01")]
        if te.empty:
            continue
        P = m.predict_proba(((te - mu) / sd).clip(-5, 5).values)[:, order]
        out.loc[te.index, ["p_best", "p_mid", "p_worst"]] = P
        out.loc[te.index, "state"] = P.argmax(axis=1)
        out.loc[te.index, "exposure"] = P @ EXP_BY_RANK
    return out


# ---------------------------------------------------------------------------------------------------------
# Live use (dashboard): the model for the current year is fitted once on data before 1 January and cached.
# ---------------------------------------------------------------------------------------------------------
def live_probs(close: pd.Series, key: str, cache_dir: str, rolling_years: int | None = None, seed: int = 7) -> dict:
    import os
    import pickle
    X = _inputs(close)
    y = X.index[-1].year
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{key}_{y}.pkl")
    if os.path.exists(path):
        c = pickle.load(open(path, "rb"))
    else:
        tr = X[X.index < f"{y}-01-01"]
        if rolling_years:
            tr = tr[tr.index >= f"{y - rolling_years}-01-01"]
        if len(tr) < 500:
            return {}
        mu, sd = tr.mean(), tr.std()
        Z = ((tr - mu) / sd).clip(-5, 5).values
        h = _fit(Z, seed + y)
        g = _fit_gmm(Z, seed + y)
        h_order = np.argsort(-(h.means_[:, 0] / np.sqrt(np.array([cv[0, 0] for cv in h.covars_]))))
        g_order = np.argsort(-(g.means_[:, 0] / np.sqrt(g.covariances_[:, 0, 0])))
        c = {"mu": mu, "sd": sd, "hmm": h, "gmm": g, "h_order": h_order, "g_order": g_order, "train_start": tr.index[0]}
        pickle.dump(c, open(path, "wb"))
    seg = X[X.index >= c["train_start"]]
    Z = ((seg - c["mu"]) / c["sd"]).clip(-5, 5).values
    ph = _forward_filter(c["hmm"], Z)[:, c["h_order"]]
    pg = c["gmm"].predict_proba(Z)[:, c["g_order"]]
    # how long has the HMM's most likely state been in place
    st = ph.argmax(axis=1)
    age = 1
    while age < len(st) and st[-1 - age] == st[-1]:
        age += 1
    names = ["Calm", "In between", "Stressed"]
    return {"date": str(seg.index[-1].date()),
            "hmm": {"p": [round(float(x), 2) for x in ph[-1]], "state": names[int(st[-1])], "days": int(age),
                    "exposure": round(float(ph[-1] @ EXP_BY_RANK), 2),
                    "p_series": [[str(d.date()), round(float(p), 3)] for d, p in zip(seg.index[-90:], ph[-90:, 2])]},
            "gmm": {"p": [round(float(x), 2) for x in pg[-1]], "state": names[int(pg[-1].argmax())],
                    "exposure": round(float(pg[-1] @ EXP_BY_RANK), 2)},
            "state_names": names}
