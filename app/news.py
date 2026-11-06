"""
News & shock watcher (2026-10-17). Runs every Market Pulse cycle on the Mac. Read-only.

Big news is rare but matters a lot, so this is built for LOW NOISE and HIGH SENSITIVITY:
  1. Market shock detector (fastest - the market reacts before headlines): Nifty / Bank Nifty move >= 1% within
     ~15 minutes or >= 2% on the day, India VIX +10% on the day. Uses Dhan live quotes (app/india.py, app/dhan_live.py).
  2. Headlines: Indian + global business news RSS, RBI and SEBI press releases, NSE corporate announcements
     for F&O stocks. Each headline gets a severity from fixed keyword tiers:
        3 = shock (war, attack, emergency / surprise policy, crash, circuit, sanctions, default, ban ...)
        2 = market-moving scheduled news (RBI policy, Fed, CPI, GDP, budget, results of big index stocks ...)
        1 = normal market news
  3. Shock mode: a severity-3 headline seen by >= 2 sources, or a market shock -> shock.active = true for 3 h,
     macOS notification at once. The desk then says "no new positions until the regime updates".
Files: data_store/agent/news.json (latest items, shock state), data_store/agent/news_seen.json (dedupe).
All news is CONTEXT - never a buy / sell signal. The event calendar is used as a risk rule only.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import updater as U

ROOT = U.ROOT
OUT = os.path.join(ROOT, "data_store", "agent", "news.json")
SEEN = os.path.join(ROOT, "data_store", "agent", "news_seen.json")
LIVE = os.path.join(ROOT, "data_store", "india", "live_quotes.json")
UNIV = os.path.join(ROOT, "data_store", "india", "universe.json")
PX_HIST = os.path.join(ROOT, "data_store", "agent", "shock_px.json")

GN = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="
FEEDS = {
    "ET Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/marketreports.xml",
    "Livemint Markets": "https://www.livemint.com/rss/markets",
    "RBI": "https://www.rbi.org.in/pressreleases_rss.xml",
    "SEBI": "https://www.sebi.gov.in/sebirss.xml",
    "Google: India markets": GN + "Nifty+OR+Sensex+OR+%22Indian+stock+market%22+when:1d",
    "Google: RBI / policy": GN + "RBI+OR+%22finance+ministry%22+OR+SEBI+when:1d",
    "Google: global markets": GN + "%22Wall+Street%22+OR+Fed+OR+%22global+markets%22+OR+crude+when:1d",
    "Google: geopolitics": GN + "war+OR+attack+OR+sanctions+OR+missile+India+OR+Pakistan+OR+China+OR+%22Middle+East%22+when:1d",
}
TIER3 = [r"\bwar\b", r"terror attack", r"\battacks? on (india|indian|oil|ships?|tankers?|refiner)", r"missile (strike|attack)s?", r"air ?strike", r"nuclear", r"emergency", r"martial law",
         r"surprise (rate )?(cut|hike)", r"unscheduled", r"emergency (meeting|rate)", r"crash", r"plunge[sd]? \d{1,2}%",
         r"lower circuit", r"trading halt", r"halted", r"sanctions? on india", r"defaults? on", r"bankrupt", r"collapse",
         r"demoneti", r"lockdown", r"pandemic", r"coup", r"assassinat", r"(rbi governor|finance minister) resign",
         r"black monday", r"bloodbath", r"meltdown", r"capital controls?", r"downgrade[sd]? india", r"market crash"]
TIER2 = [r"\brbi\b.*(policy|repo|rate)", r"repo rate", r"\bmpc\b", r"\bfed\b", r"fomc", r"powell", r"\bcpi\b", r"inflation",
         r"\bgdp\b", r"budget", r"\bfii\b|\bfpi\b", r"results?|q[1-4] ?(fy)?\d*|earnings|profit", r"sebi", r"f&o", r"expiry",
         r"crude", r"rupee", r"bond yield", r"rating", r"block deal", r"pledge", r"\bipo\b", r"election", r"tariff",
         r"sanction", r"\bban(s|ned)?\b", r"resign", r"circuit", r"default"]
INDEX_NAMES = {"NIFTY 50": "Nifty", "Nifty 50": "Nifty", "Nifty Bank": "Bank Nifty"}


def _hash(s):
    return hashlib.md5(s.encode("utf-8", "ignore")).hexdigest()[:16]


# 2026-11-03 (Nikhil: "news is noise"): crime, film, sport and defence-test stories are dropped unless they mention markets
NOISE = [r"\bfilm\b", r"actor|actress|bollywood|celebrit|star kid", r"cricket|\bipl\b|world cup|olympic", r"\bpolice\b|murder|encounter|chainsaw|amputat|\brape\b|arrested|stabb|gang",
         r"test[- ]?fire|successfully test|\bdrdo\b|su-57|fighter jet|brahmos|pralay|stealth", r"horoscope|recipe|wedding|viral video", r"steps up attack|hits out|slams|jibe|takes a dig|war of words"]
MARKET = r"market|stocks?\b|shares?\b|sensex|nifty|rupee|\boil\b|crude|bond|yield|econom|tariff|\btrade\b|\bfii\b|\bfpi\b|\brbi\b|\bfed\b|investor|export|import|\bgdp\b|inflation|sebi|ipo"
GEO_STRONG = r"\bwar (fear|risk|escalat|threat)|outbreak of war|declares? war|ceasefire|sanction|air ?strike|nuclear|blockade|hormuz|missile (strike|attack)"


# 2026-11-04 (Nikhil): news sentiment score for the News & events card - word-based tone of each headline (market wording),
# weighted by how big the story is (severity, number of sources) and how recent (12-hour half-life). -10 = very negative, +10 = very positive.
POS = [r"\brall(y|ies|ied)\b", r"\bsurg(e|es|ed|ing)\b", r"\bjump(s|ed)?\b", r"\bsoar(s|ed)?\b", r"\bgain(s|ed)?\b", r"\brise[sn]?\b|\brising\b|\brose\b",
       r"\bclimb(s|ed)?\b", r"\brecord high\b|\ball-time high\b|\bfresh high\b", r"\brebound(s|ed)?\b", r"\brecover(s|y|ed)?\b", r"\bupgrade[sd]?\b",
       r"\bbeat(s)? (estimates|expectations)\b|\bbeats\b", r"\bstrong(er)?\b", r"\boptimis", r"\bboost(s|ed)?\b", r"\bceasefire\b|\bpeace\b|\btruce\b",
       r"\bdeal\b|\bagreement\b", r"\binflows?\b", r"\b(rate|rates) cut\b|\bcuts? rates?\b", r"\beas(e|es|ed|ing)\b", r"\bbullish\b", r"\bupbeat\b", r"\bcools?\b"]
NEG = [r"\bfall(s|ing)?\b|\bfell\b", r"\bplung(e|es|ed)\b", r"\bslump(s|ed)?\b", r"\bcrash(es|ed)?\b", r"\btumbl(e|es|ed)\b", r"\bslid(e|es)?\b|\bslip(s|ped)?\b",
       r"\bdrop(s|ped)?\b", r"\bsell-?off\b|\bsell off\b", r"\bloss(es)?\b|\blose[s]?\b", r"\bwar\b", r"\bstrikes?\b|\battack", r"\bsanction", r"\btariff",
       r"\bfears?\b|\bworr(y|ies|ied)\b|\bjitter", r"\brecession\b", r"\boutflows?\b", r"\bdowngrade[sd]?\b", r"\bmiss(es|ed)?\b", r"\bweak(er|ness)?\b",
       r"\bslowdown\b|\bslow(s|ing)?\b", r"\bhike[sd]?\b", r"\bdefault", r"\bescalat", r"\bbearish\b", r"\bpressure\b", r"\btension", r"\bcrisis\b", r"\bvolatil"]
INDIA_RX = r"india|nifty|sensex|\brbi\b|rupee|sebi|\bfii|\bfpi|dalal|\bnse\b|\bbse\b|mumbai|crore"
INDIA_SRC = ("ET Markets", "Moneycontrol", "Livemint Markets", "RBI", "SEBI", "Google: India markets", "Google: RBI / policy")


def tone(title: str) -> int:
    t = title.lower()
    # oil / crude price headlines: the direction of oil decides - falling oil helps equities (India imports most of its oil)
    if re.search(r"\b(crude|oil|brent|wti)\b", t) and re.search(r"\bpric|\bfutures\b|\bbarrel|\bbrent|\bwti", t) and not re.search(r"escalat|\bstrikes?\b|\battack", t):
        up = re.search(r"\bjump|\bsurg|\bris(e|es|ing)\b|\brose\b|\bsoar|\bclimb|\brall|\bspike|\bhigher\b", t)
        dn = re.search(r"\bfall|\bfell\b|\bdrop|\bslid|\bslump|\bdip|\beas(e|es|ed|ing)\b|\blower\b|\btumbl|\bplung", t)
        if up and not dn:
            return -1
        if dn and not up:
            return 1
    p = sum(1 for r in POS if re.search(r, t)); n = sum(1 for r in NEG if re.search(r, t))
    if re.search(r"lower circuit|52-week low", t):
        n += 2
    if re.search(r"\bzoom(s|ed)?\b|\bupper circuit\b|\b52-week high\b", t):
        p += 1
    return max(-2, min(2, p - n))


def sentiment(items: list, now=None) -> dict:
    import math
    now = now or datetime.now()
    acc = {"all": [0.0, 0.0], "india": [0.0, 0.0], "global": [0.0, 0.0]}
    cnt = {"pos": 0, "neg": 0, "flat": 0}
    scored = []
    for g in items:
        if g.get("source") == "NSE announcement":
            continue
        try:
            age_h = max(0.0, (now - datetime.strptime(str(g.get("published"))[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600)
        except Exception:
            age_h = 24.0
        if age_h > 48:
            continue
        tn = tone(g["title"])
        w = (int(g.get("severity") or 1)) * min(int(g.get("n_sources") or 1), 3) * math.pow(0.5, age_h / 12)
        reg = "india" if (re.search(INDIA_RX, g["title"].lower()) or g.get("source") in INDIA_SRC) else "global"
        for k in ("all", reg):
            acc[k][0] += w * tn; acc[k][1] += w
        cnt["pos" if tn > 0 else "neg" if tn < 0 else "flat"] += 1
        if tn:
            scored.append((w * tn, g["title"], g.get("link"), reg))
    sc = lambda k: round(10 * acc[k][0] / acc[k][1] / 2, 1) if acc[k][1] else None
    lab = lambda v: None if v is None else "Positive" if v >= 2 else "Negative" if v <= -2 else "Mixed"
    scored.sort()
    return {"score": sc("all"), "label": lab(sc("all")), "india": sc("india"), "global": sc("global"), "counts": cnt,
            "most_neg": [{"title": t, "link": l, "region": r} for _, t, l, r in scored[:3]],
            "most_pos": [{"title": t, "link": l, "region": r} for _, t, l, r in scored[::-1][:3] if _ > 0],
            "at": now.strftime("%Y-%m-%d %H:%M"),
            "method": "Word-based tone of each headline (rally / gains vs falls / war / fears ...), weighted by story size and recency (12 h half-life), last 48 h. -10 very negative, +10 very positive."}


def relevant(title: str, source: str = "") -> bool:
    t = title.lower()
    if any(re.search(p, t) for p in NOISE) and not re.search(MARKET, t):
        return False
    if source.startswith("Google: geopolitics") and not (re.search(MARKET, t) or re.search(GEO_STRONG, t)):
        return False
    return True


def severity(title: str) -> int:
    t = title.lower()
    if any(re.search(p, t) for p in TIER3):
        return 3
    if any(re.search(p, t) for p in TIER2):
        return 2
    return 1


def _norm_title(t):
    return re.sub(r"\s+-\s+[^-]{2,40}$", "", t).strip()      # Google News appends " - Publisher"


def parse_rss(raw: bytes, source: str) -> list:
    out = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate") or ""
        try:
            ts = parsedate_to_datetime(pub).astimezone().replace(tzinfo=None) if pub else None
        except Exception:
            ts = None
        out.append({"source": source, "title": _norm_title(title), "link": link,
                    "published": ts.strftime("%Y-%m-%d %H:%M") if ts else None})
    return out


def fetch_feeds() -> tuple[list, list]:
    items, errs = [], []
    for name, url in FEEDS.items():
        try:
            items += parse_rss(U.http_get(url, timeout=20, tries=2), name)[:40]
        except Exception as e:
            errs.append(f"{name}: {str(e)[:80]}")
        time.sleep(0.2)
    return items, errs


def fetch_nse_announcements(symbols: set) -> tuple[list, list]:
    """NSE corporate announcements (needs the nseindia.com cookie first)."""
    try:
        hdr = {"Accept": "application/json", "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements"}
        if U._CSESSION is not None:        # 2026-10-23 audit: browser-like session (plain requests get HTTP 403)
            U._CSESSION.get("https://www.nseindia.com/companies-listing/corporate-filings-announcements", timeout=20)
            r = U._CSESSION.get("https://www.nseindia.com/api/corporate-announcements?index=equities", headers=hdr, timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            rows = r.json()
        else:
            U.http_get("https://www.nseindia.com/", headers={"Accept": "text/html"}, timeout=15, tries=1)
            rows = json.loads(U.http_get("https://www.nseindia.com/api/corporate-announcements?index=equities", headers=hdr, timeout=20, tries=2))
    except Exception as e:
        return [], [f"NSE announcements: {str(e)[:80]}"]
    out = []
    for r in rows if isinstance(rows, list) else rows.get("data", []):
        sym = r.get("symbol")
        if symbols and sym not in symbols:
            continue
        title = f"{sym}: {r.get('desc') or ''} - {(r.get('attchmntText') or '')[:160]}".strip()
        ts = r.get("an_dt") or r.get("sort_date")
        try:
            ts = datetime.strptime(ts, "%d-%b-%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
        out.append({"source": "NSE announcement", "title": title, "link": r.get("attchmntFile"), "published": ts, "symbol": sym})
    return out[:80], []


def _fresh_index_quotes() -> dict | None:
    """Nifty / Bank Nifty / India VIX straight from Dhan (one small call), so the shock check is not 5 minutes late."""
    try:
        import dhan_live as DL
        if not DL.ok():
            return None
        ids = {"13": "Nifty 50", "25": "Nifty Bank", "21": "India VIX"}
        rows = (DL.quote({"IDX_I": [13, 25, 21]}).get("IDX_I") or {})
        out = {}
        for sid, r in rows.items():
            f = DL.row_fields(r)
            if ids.get(str(sid)) and f["ltp"] is not None and f["prev_close"] is not None:
                out[ids[str(sid)]] = {"ltp": f["ltp"], "prev_close": f["prev_close"]}
        return {"market_open": True, "quotes": out} if out else None
    except Exception:
        return None


def market_shock() -> dict | None:
    """Fast detector from live quotes: big index moves or a VIX jump."""
    n = datetime.now()
    if not (n.weekday() < 5 and 9 * 60 + 15 <= n.hour * 60 + n.minute <= 15 * 60 + 30):
        return None
    q = _fresh_index_quotes()
    if q is None:
        try:
            q = json.load(open(LIVE))
        except Exception:
            return None
    if not q.get("market_open"):
        return None
    now = time.time()
    hist = json.load(open(PX_HIST)) if os.path.exists(PX_HIST) else {}
    reasons = []
    for key, label in (("Nifty 50", "Nifty"), ("Nifty Bank", "Bank Nifty")):
        v = q["quotes"].get(key)
        if not v or not v.get("ltp") or not v.get("prev_close"):
            continue
        day = v["ltp"] / v["prev_close"] - 1
        h = [x for x in hist.get(key, []) if now - x[0] <= 20 * 60] + [[now, v["ltp"]]]
        hist[key] = h
        old = h[0][1]
        fast = v["ltp"] / old - 1 if old else 0
        if abs(fast) >= 0.01 and now - h[0][0] >= 5 * 60:
            reasons.append(f"{label} {fast*100:+.1f}% in {int((now-h[0][0])/60)} min")
        if abs(day) >= 0.02:
            reasons.append(f"{label} {day*100:+.1f}% on the day")
    vix = q["quotes"].get("India VIX")
    if vix and vix.get("ltp") and vix.get("prev_close") and vix["ltp"] / vix["prev_close"] - 1 >= 0.10:
        reasons.append(f"India VIX {100*(vix['ltp']/vix['prev_close']-1):+.0f}% today")
    with open(PX_HIST, "w") as f:
        json.dump(hist, f)
    return {"reasons": reasons} if reasons else None


def notify(title, msg):
    msg = msg.replace('"', "'")[:220]
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}" sound name "Sosumi"'],
                       timeout=5, capture_output=True)
    except Exception:
        pass


def _wake(triggers):
    """Instant News Watcher run through the Claude API (only if a key is saved; rate- and budget-limited)."""
    try:
        import threading
        import agent_runner
        if agent_runner.has_key():
            threading.Thread(target=agent_runner.instant_news, args=(triggers,), daemon=True, name="news_instant").start()
    except Exception:
        pass


def run_all() -> dict:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    def _safe(path):                       # 2026-11-03: a half-written file (two writers) stopped all news updates - never crash on it
        try:
            return json.load(open(path)) if os.path.exists(path) else {}
        except Exception:
            return {}
    prev, seen = _safe(OUT), _safe(SEEN)
    univ = json.load(open(UNIV)) if os.path.exists(UNIV) else {}
    items, errs = fetch_feeds()
    ann, e2 = fetch_nse_announcements(set((univ.get("stocks") or {}).keys()))
    items += ann
    errs += e2
    now = datetime.now()
    # 2026-10-31 (Nikhil): never keep news older than 2 days (items with no date are kept)
    _cut = (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    items = [it for it in items if not it.get("published") or str(it["published"])[:16] >= _cut]
    items = [it for it in items if it.get("source") == "NSE announcement" or relevant(it["title"], it.get("source", ""))]
    # dedupe across sources by normalised title; count how many sources carry the same story
    groups = {}
    for it in items:
        k = _hash(re.sub(r"[^a-z0-9 ]", "", it["title"].lower())[:90])
        g = groups.setdefault(k, {**it, "sources": set(), "severity": severity(it["title"])})
        g["sources"].add("Google News" if it["source"].startswith("Google") else it["source"])
    fresh = []
    for k, g in groups.items():
        g["sources"] = sorted(g["sources"])
        g["n_sources"] = len(g["sources"])
        g["new"] = k not in seen
        g["id"] = k
        if g["new"]:
            seen[k] = now.strftime("%Y-%m-%d %H:%M")
        fresh.append(g)
    # keep only the last 36 hours of seen ids
    cutoff = (now - timedelta(hours=36)).strftime("%Y-%m-%d %H:%M")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    tmp_s = f"{SEEN}.{os.getpid()}.{threading.get_ident()}.tmp"     # atomic write, like news.json
    with open(tmp_s, "w") as f:
        json.dump(seen, f)
    os.replace(tmp_s, SEEN)
    fresh.sort(key=lambda g: (g["severity"], g["n_sources"], g.get("published") or ""), reverse=True)
    # shock mode
    shock = prev.get("shock") or {"active": False}
    if shock.get("active") and shock.get("until") and shock["until"] < now.strftime("%Y-%m-%d %H:%M"):
        shock = {"active": False, "ended": now.strftime("%Y-%m-%d %H:%M")}
    triggers = []
    for g in fresh:
        if g["new"] and g["severity"] == 3 and (g["n_sources"] >= 2 or g["source"] in ("RBI", "SEBI")):
            triggers.append(f"Headline: {g['title'][:120]} ({', '.join(g['sources'][:3])})")
    ms = market_shock()
    if ms:
        triggers += [f"Market: {r}" for r in ms["reasons"]]
    if triggers:
        # 2026-11-06 (Nikhil: "repeated... Market: India VIX +10% today"): the same market condition re-triggered every cycle and was appended,
        # notified and sent to the agents each time. Now one line per trigger: a market trigger is keyed by its label (text before the number,
        # e.g. "Market: India VIX"), so a changed number updates that line in place; alerts / agent wake-ups only for triggers that are NEW.
        import re as _re
        key = lambda r: _re.split(r"\s[+-]?\d", r, 1)[0] if r.startswith("Market:") else r
        old = list(shock.get("reasons", [])) if shock.get("active") else []
        dedup, seen_k = [], {}
        for r in old:
            k = key(r)
            if k in seen_k: dedup[seen_k[k]] = r
            else: seen_k[k] = len(dedup); dedup.append(r)
        new_t = []
        for r in triggers:
            k = key(r)
            if k in seen_k: dedup[seen_k[k]] = r
            else: seen_k[k] = len(dedup); dedup.append(r); new_t.append(r)
        shock = {"active": True, "since": shock.get("since") if shock.get("active") else now.strftime("%Y-%m-%d %H:%M"),
                 "until": (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"), "reasons": dedup[-8:]}
        if new_t:
            notify("Synaptic - BIG NEWS / SHOCK", " | ".join(new_t)[:220])
            _wake(new_t)
    elif any(g["new"] and g["severity"] == 3 for g in fresh):
        g = next(g for g in fresh if g["new"] and g["severity"] == 3)
        notify("Synaptic - possible big news (1 source)", g["title"][:200])
        _wake([f"Possible big news (1 source, unconfirmed): {g['title'][:140]} ({g['source']})"])
    try:
        sent = sentiment(fresh, now)
        hist_p = os.path.join(os.path.dirname(OUT), "news_sentiment_hist.json")
        try:
            hist = json.load(open(hist_p))
        except Exception:
            hist = []
        if sent["score"] is not None and (not hist or hist[-1]["t"][:15] != sent["at"][:15]):       # one point per 10 minutes
            hist.append({"t": sent["at"], "v": sent["score"], "in": sent["india"], "gl": sent["global"]})
        cut7 = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        hist = [h for h in hist if h["t"] >= cut7]
        tmp_h = hist_p + f".{os.getpid()}.tmp"
        with open(tmp_h, "w") as f:
            json.dump(hist, f)
        os.replace(tmp_h, hist_p)
        day_ago = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
        old = [h for h in hist if h["t"] <= day_ago]
        sent["chg_24h"] = round(sent["score"] - old[-1]["v"], 1) if old and sent["score"] is not None else None
        sent["series"] = [h["v"] for h in hist[::3]][-80:]
    except Exception as e:
        sent = {"error": str(e)[:100]}
    out = {"at": now.strftime("%Y-%m-%d %H:%M:%S"), "shock": shock, "sentiment": sent,
           "top": [g for g in fresh if g["severity"] >= 2][:25],
           "shock_items": [g for g in fresh if g["severity"] == 3][:10],
           "nse_announcements": [g for g in fresh if g["source"] == "NSE announcement"][:30],
           "counts": {"items": len(fresh), "severity3": sum(g["severity"] == 3 for g in fresh),
                      "severity2": sum(g["severity"] == 2 for g in fresh)},
           "errors": errs[:6], "label": "context"}
    tmp = f"{OUT}.{os.getpid()}.{threading.get_ident()}.tmp"   # unique per writer (two threads can save at once)
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1, default=list)
    os.replace(tmp, OUT)
    return {"items": len(fresh), "shock": shock.get("active"), "errors": len(errs)}


JOBS = [("news", run_all)]

if __name__ == "__main__":
    print(json.dumps(run_all(), indent=1))
