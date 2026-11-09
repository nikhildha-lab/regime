"""TAB NEWS - swing-trade catalysts per market tab (2026-11-04, Nikhil: "a new section ... news very centric to the
respective tab, domestic stock news worth taking swing trades ... don't include everything, filter what is critical and
can make the stock move in some direction ... same for the other tabs").

For each tab (india, us, crypto, metals, global = commodities) it reads a few RSS feeds and keeps ONLY headlines that
  1. name something on that tab (an F&O stock, a US mega-cap, one of our coins, a metal, crude / natural gas), and
  2. carry a known price catalyst (results beat / miss, order win, deal, rating change, regulator action, guidance,
     management exit, ETF flows, hack, OPEC, inventories, supply disruption ...).
Each kept item gets a direction (up / down / either), a catalyst label and a strength (catalyst weight x sources x
recency). At most 2 items per name, 10 per tab, last 48 h. Written to data_store/agent/tab_news.json every 10 minutes
by the news loop. Context for Nikhil's own judgement - not a signal, never an order.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timedelta

import updater as U
import news as N

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "data_store", "agent", "tab_news.json")
UNIV = os.path.join(ROOT, "data_store", "india", "universe.json")
EVERY = 600

GN_IN = "https://news.google.com/rss/search?hl=en-IN&gl=IN&ceid=IN:en&q="
GN_US = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
FEEDS = {
    "india": {"ET Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
              "ET Stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
              "Livemint Markets": "https://www.livemint.com/rss/markets",
              "Google: India stocks": GN_IN + "shares+(order+OR+results+OR+acquisition+OR+upgrade+OR+downgrade+OR+SEBI+OR+buyback+OR+target)+when:1d"},
    "us": {"CNBC": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
           "CNBC Earnings": "https://www.cnbc.com/id/15839135/device/rss/rss.html",
           "Google: US stocks": GN_US + "stock+(upgrade+OR+downgrade+OR+guidance+OR+earnings+OR+acquisition+OR+FTC+OR+DOJ)+when:1d"},
    "crypto": {"CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
               "Cointelegraph": "https://cointelegraph.com/rss",
               "Google: crypto": GN_US + "(bitcoin+OR+ethereum+OR+solana+OR+XRP+OR+chainlink+OR+uniswap+OR+zcash)+(ETF+OR+SEC+OR+hack+OR+listing+OR+upgrade+OR+treasury)+when:1d"},
    "metals": {"Google: metals": GN_US + "(gold+OR+silver+OR+copper+OR+aluminium+OR+aluminum)+price+when:1d"},
    "global": {"Google: energy": GN_US + "(crude+oil+OR+OPEC+OR+Brent+OR+%22natural+gas%22+OR+LNG)+when:1d"},
}

# (label, regex on the lower-case headline, direction +1 up / -1 down / 0 either, weight)
CAT_STOCK = [
    ("Order win", r"\b(bags|wins|secures|receives|bagged|won|secured|lands|bag)\b.{0,40}\b(orders?|contracts?|deals?|projects?)\b|\border (win|inflow)", 1, 3),
    ("Results beat", r"(profit|net income|pat|ebitda|revenue|earnings|sales)\b.{0,40}\b(jumps|surges|rises|soars|climbs|grows|doubles|triples|up \d|beats?)\b|\bbeats? (estimates|expectations|street|forecasts)", 1, 3),
    ("Results miss", r"(profit|net income|pat|ebitda|revenue|earnings|sales)\b.{0,40}\b(falls|drops|declines|slumps|plunges|slides|down \d|misses|halves)\b|\bmiss(es|ed)? (estimates|expectations|street)|\bloss widens|\bnet loss\b", -1, 3),
    ("Upgrade", r"\bupgrade[sd]?\b|\braises? (its |the )?(price )?target|\btarget (price )?(raised|hiked)|\boverweight\b|\b(initiates?|starts?) .{0,20}\bbuy\b", 1, 2.5),
    ("Downgrade", r"\bdowngrade[sd]?\b|\bcuts? (its |the )?(price )?target|\btarget (price )?(cut|lowered|slashed)|\bunderweight\b", -1, 2.5),
    ("Guidance raised", r"\b(raises|ups|hikes|lifts) .{0,15}(guidance|outlook|forecast)", 1, 3),
    ("Guidance cut", r"\b(cuts|lowers|slashes|trims) .{0,15}(guidance|outlook|forecast)|\bwarns? (on|of) (profit|sales|revenue|earnings|demand|margins?)", -1, 3),
    ("Buyback", r"\bbuy-?back\b", 1, 2.5),
    ("Deal / acquisition", r"\bacquir|\bacquisition\b|\bmerger\b|\bto buy\b.{0,30}\bstake|\bstake (buy|purchase)", 0, 2),
    ("Approval", r"\b(usfda|fda|ema|dcgi|cci|nclt|sebi|rbi)\b.{0,40}\b(approv|nod|clear)|\bapproval for\b", 1, 2.5),
    ("Regulator action", r"\b(sebi|rbi|\bed\b|cbi|income tax|sec|doj|ftc|eu commission|antitrust)\b.{0,40}\b(order|penalty|fine[sd]?|probe|raid|ban|notice|search|investigat|sues|lawsuit)|\bwarning letter\b|\bimport alert\b", -1, 3),
    ("Promoter / block sale", r"\bblock deal\b|\bpromoters?\b.{0,30}\b(sell|sells|pledge|offload|trim)|\bstake sale\b|\boffloads?\b", -1, 2),
    ("Management exit", r"\b(ceo|cfo|md|chairman|managing director)\b.{0,30}\b(resigns?|quits|steps down|exit)", -1, 2),
    ("Default / fraud", r"\bdefault|\bfraud\b|\binsolvency\b|\bbankrupt", -1, 3),
    ("Plant / outage", r"\bfire at\b|\bplant (shut|closure)|\bshutdown\b|\boutage\b|\brecall[s]?\b", -1, 2),
    ("Big move", r"\b(shares?|stock)\b.{0,30}\b(soar|surge|zoom|jump|rall|plunge|crash|tank|slump|tumble|hit (upper|lower) circuit)|\b(upper|lower) circuit\b|\b52-week (high|low)\b", 0, 1.5),
]
CAT_CRYPTO = [
    ("ETF flows", r"\betf\b.{0,40}\b(inflow|record|approv|launch|filing)", 1, 3),
    ("ETF outflows", r"\betf\b.{0,40}\boutflow", -1, 3),
    ("Regulation", r"\b(sec|cftc|doj|regulator|ban|crackdown)\b.{0,40}\b(sues|lawsuit|charges|ban|probe|crackdown)", -1, 3),
    ("Regulatory relief", r"\b(sec|cftc|court)\b.{0,40}\b(drops|dismiss|approv|clarity|wins?)", 1, 3),
    ("Hack / exploit", r"\bhack|\bexploit|\bdrained\b|\bstolen\b", -1, 3),
    ("Listing", r"\blist(s|ing|ed)\b|\badds support\b", 1, 2),
    ("Delisting", r"\bdelist", -1, 2.5),
    ("Token unlock", r"\bunlock", -1, 2),
    ("Treasury / whale buy", r"\b(treasury|whale|institution|company)\b.{0,40}\b(buys|bought|adds|accumulat)", 1, 2),
    ("Network upgrade", r"\b(upgrade|hard fork|mainnet|launch)\b", 1, 1.5),
    ("Liquidations", r"\bliquidat", -1, 1.5),
    ("Big move", r"\b(soar|surge|jump|rall|plunge|crash|tumble|slump|record high|all-time high)", 0, 1.5),
]
CAT_COMMOD = [
    ("OPEC / supply", r"\bopec\+?\b.{0,40}\b(cut|curb|reduce)|\bsupply (disruption|outage|cut)|\bsanction|\bpipeline\b.{0,20}\b(attack|shut|halt)|\bstrait of hormuz\b|\bhurricane\b", 1, 3),
    ("OPEC / more supply", r"\bopec\+?\b.{0,40}\b(hike|raise|increase|boost)|\boutput (hike|increase)|\bglut\b|\boversupply\b", -1, 3),
    ("Inventories", r"\b(inventor|stockpile)\w*\b.{0,30}\b(draw|fall|drop|decline)", 1, 2),
    ("Inventories build", r"\b(inventor|stockpile)\w*\b.{0,30}\b(build|rise|jump|swell)", -1, 2),
    ("Central-bank buying", r"\bcentral banks?\b.{0,30}\b(buy|purchas|add)", 1, 2.5),
    ("Rates / dollar", r"\b(fed|rate cut|rate hike|dollar|yields?)\b", 0, 1.5),
    ("Demand", r"\b(demand|china|imports?)\b.{0,30}\b(surge|jump|rise|record|weak|slump|fall)", 0, 1.5),
    ("Tariffs", r"\btariff", 0, 2),
    ("Ceasefire / talks", r"\bceasefire\b|\btruce\b|\bpeace (talks|deal)\b|\btalks\b|\bde-?escalat", -1, 2),   # less risk premium in oil / gold
    ("War / supply risk", r"\bwar\b|\bstrike[sd]?\b|\battack|\bescalat|\bhormuz\b", 1, 2),
    ("Big move", r"\b(soar|surge|jump|rall|plunge|crash|tumble|slump|record high|all-time high|lowest|highest)", 0, 1.5),
]
NSE_ANN = [("Order win", r"order|contract|letter of award|loa\b", 1, 3), ("Deal / acquisition", r"acquisition|amalgamation|scheme of arrangement", 0, 2),
           ("Buyback", r"buy ?back", 1, 2.5), ("Rating", r"credit rating", 0, 1.5), ("Management exit", r"resignation", -1, 2),
           ("Regulator action", r"penalty|show cause|regulatory action|search|investigation", -1, 3), ("Fund raise", r"fund raising|qip|preferential issue|rights issue", 0, 1.5)]

US_NAMES = {"AAPL": ["apple"], "MSFT": ["microsoft"], "NVDA": ["nvidia"], "AMZN": ["amazon"], "GOOGL": ["alphabet", "google"], "META": ["meta platforms", "meta"],
            "TSLA": ["tesla"], "AVGO": ["broadcom"], "ORCL": ["oracle"], "AMD": ["amd", "advanced micro"], "NFLX": ["netflix"], "JPM": ["jpmorgan", "jp morgan"],
            "BAC": ["bank of america"], "GS": ["goldman sachs"], "MS": ["morgan stanley"], "WFC": ["wells fargo"], "C": ["citigroup"], "V": ["visa"], "MA": ["mastercard"],
            "LLY": ["eli lilly", "lilly"], "UNH": ["unitedhealth"], "JNJ": ["johnson & johnson"], "PFE": ["pfizer"], "MRK": ["merck"], "ABBV": ["abbvie"], "NVO": ["novo nordisk"],
            "XOM": ["exxon"], "CVX": ["chevron"], "WMT": ["walmart"], "COST": ["costco"], "HD": ["home depot"], "MCD": ["mcdonald"], "NKE": ["nike"], "SBUX": ["starbucks"],
            "DIS": ["disney"], "KO": ["coca-cola"], "PEP": ["pepsico"], "PG": ["procter"], "BA": ["boeing"], "CAT": ["caterpillar"], "GE": ["ge aerospace", "general electric"],
            "INTC": ["intel"], "MU": ["micron"], "QCOM": ["qualcomm"], "TXN": ["texas instruments"], "ARM": ["arm holdings"], "TSM": ["tsmc", "taiwan semiconductor"],
            "ASML": ["asml"], "PLTR": ["palantir"], "CRM": ["salesforce"], "ADBE": ["adobe"], "NOW": ["servicenow"], "IBM": ["ibm"], "UBER": ["uber"], "ABNB": ["airbnb"],
            "PYPL": ["paypal"], "COIN": ["coinbase"], "MSTR": ["microstrategy", "strategy inc"], "HOOD": ["robinhood"], "SMCI": ["super micro"], "DELL": ["dell"],
            "SNOW": ["snowflake"], "CRWD": ["crowdstrike"], "PANW": ["palo alto"], "SHOP": ["shopify"], "BABA": ["alibaba"], "F": ["ford"], "GM": ["general motors"],
            "RIVN": ["rivian"], "UPS": ["ups"], "FDX": ["fedex"], "T": ["at&t"], "VZ": ["verizon"], "CMCSA": ["comcast"]}
COINS = {"BTC": ["bitcoin", "btc"], "ETH": ["ethereum", "ether", "eth"], "SOL": ["solana"], "BNB": ["bnb", "binance coin"], "XRP": ["xrp", "ripple"],
         "UNI": ["uniswap"], "ZEC": ["zcash"], "LINK": ["chainlink"], "DASH": ["dash"], "AVAX": ["avalanche", "avax"], "CRYPTO": ["crypto", "stablecoin", "digital asset"]}
METALS = {"GOLD": ["gold"], "SILVER": ["silver"], "COPPER": ["copper"], "ALUMINIUM": ["aluminium", "aluminum"], "PLATINUM": ["platinum"]}
ENERGY = {"CRUDE": ["crude", "oil", "brent", "wti", "opec"], "NATGAS": ["natural gas", "lng", "gas prices"]}
GENERIC = {"india", "indian", "bank", "power", "energy", "finance", "capital", "life", "global", "steel", "cement", "industries", "motors",
           "united", "national", "state", "oil", "gas", "infra", "tech", "one", "max", "info", "titan", "trent", "dash"}
OPINION = re.compile(r"^(will|should|can|is|are|what|why|how|does|do)\b|\?\s*$|price prediction|reasons? why|should (you|investors)|stocks? to (buy|watch)|top picks|"
                     r"what to do|explained|here'?s (why|what|how)|things to know|key takeaways|live updates?|market wrap|stock market today", re.I)
REACT_UP = re.compile(r"\b(shares?|stocks?)\b.{0,25}\b(rise|rises|rose|jump|jumps|jumped|surge|surges|surged|soar|soars|rall(y|ies|ied)|gain|gains|climb|climbs|zoom|zooms|up \d)", re.I)
REACT_DN = re.compile(r"\b(shares?|stocks?)\b.{0,25}\b(fall|falls|fell|drop|drops|dropped|slump|slumps|tumble|tumbles|tumbled|plunge|plunges|sink|sinks|slide|slides|tank|tanks|down \d)", re.I)
_C: dict = {}


def _toks(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{4,}", t.lower())} - {"shares", "stock", "stocks", "results", "price", "after", "with", "from", "says", "this", "that"}


def _india_names() -> dict:
    x = _C.get("in_names")
    if x:
        return x
    out = {}
    try:
        st = json.load(open(UNIV)).get("stocks") or {}
    except Exception:
        st = {}
    for sym, v in st.items():
        if "TEST" in sym:
            continue
        nm = (v.get("name") or "").lower().replace(" ltd", "").replace(" limited", "").strip()
        al = {nm} if nm else set()
        short = re.sub(r"\b(industries|corporation|company|enterprises|india|of india|ltd)\b", "", nm).strip()
        if len(short) >= 4 and short not in GENERIC:
            al.add(short)
        if len(sym) >= 3 and sym.lower() not in GENERIC:
            al.add(sym.lower())
        out[sym] = sorted(a for a in al if a and a not in GENERIC)
    _C["in_names"] = out
    return out


def _who(title: str, names: dict) -> list:
    t = " " + title.lower() + " "
    hit = []
    for k, al in names.items():
        for a in al:
            if re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", t):
                hit.append(k)
                break
    return hit


def _cat(title: str, cats: list):
    t = title.lower()
    best = None
    for lab, rx, d, w in cats:
        if re.search(rx, t) and (best is None or w > best[3]):
            best = (lab, rx, d, w)
    return best


def _fetch(tab: str) -> list:
    items = []
    for name, url in FEEDS[tab].items():
        try:
            items += N.parse_rss(U.http_get(url, timeout=15, tries=1), name)[:60]
        except Exception as e:
            items.append({"error": f"{name}: {str(e)[:60]}"})
        time.sleep(0.2)
    return items


def build_tab(tab: str, now: datetime, extra: list | None = None) -> dict:
    raw = _fetch(tab) + (extra or [])
    errs = [x["error"] for x in raw if "error" in x]
    raw = [x for x in raw if "title" in x]
    names = {"india": _india_names(), "us": US_NAMES, "crypto": COINS, "metals": METALS, "global": ENERGY}[tab]
    cats = {"india": CAT_STOCK, "us": CAT_STOCK, "crypto": CAT_CRYPTO, "metals": CAT_COMMOD, "global": CAT_COMMOD}[tab]
    cut = (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
    groups = {}
    for it in raw:
        if it.get("published") and it["published"] < cut:
            continue
        if not N.relevant(it["title"], it.get("source", "")) or OPINION.search(it["title"].strip()):
            continue
        ann = it.get("source") == "NSE announcement"
        who = [it["title"].split(":")[0].strip().upper()] if ann else _who(it["title"], names)
        if not who:
            continue
        c = _cat(it["title"], NSE_ANN if ann else cats)
        if not c:
            continue
        lab, _, d, w = c
        if not ann and tab in ("india", "us"):            # the stock's own reaction, when the headline gives it, decides the arrow
            up, dn = REACT_UP.search(it["title"]), REACT_DN.search(it["title"])
            if up and not dn:
                d = 1
            elif dn and not up:
                d = -1
        if tab in ("metals", "global", "crypto"):        # the named asset's own move decides the arrow ("gold gains, silver slips")
            tl = it["title"].lower()
            for a in names.get(who[0], []):
                m = re.search(re.escape(a) + r"\b(?:\s+\w+){0,3}?\s+(price[s]?\s+)?(gain|gains|rise|rises|rose|jump|jumps|surge|surges|climb|climbs|rall\w*|soar\w*|up\b|fall|falls|fell|drop|drops|slip|slips|slide|slides|slump\w*|tumble\w*|plunge\w*|crash\w*|down\b|ease|eases)", tl)
                if m:
                    d = 1 if re.match(r"gain|rise|rose|jump|surge|climb|rall|soar|up", m.group(2)) else -1
                    break
        if d == 0 and not ann:
            tn = N.tone(it["title"])
            d = 1 if tn > 0 else -1 if tn < 0 else 0
        key = hashlib.md5(re.sub(r"[^a-z0-9 ]", "", it["title"].lower())[:80].encode()).hexdigest()[:12]
        g = groups.setdefault(key, {"title": it["title"], "link": it.get("link"), "published": it.get("published"), "who": who[:3],
                                    "catalyst": lab, "dir": d, "w": w, "sources": set()})
        g["sources"].add(it.get("source", ""))
    out = []
    for g in groups.values():
        try:
            age_h = max(0.0, (now - datetime.strptime(str(g["published"])[:16], "%Y-%m-%d %H:%M")).total_seconds() / 3600)
        except Exception:
            age_h = 12.0
        g["strength"] = round(g["w"] * min(len(g["sources"]), 3) * math.pow(0.5, age_h / 24), 2)
        g["sources"] = sorted(g["sources"])
        g.pop("w")
        out.append(g)
    out.sort(key=lambda g: (-g["strength"], g.get("published") or ""), reverse=False)
    per, keep = {}, []
    for g in out:
        k = g["who"][0]
        if per.get(k, 0) >= 2:
            continue
        tk = _toks(g["title"])                         # same story told by several outlets -> keep the strongest once
        dup = next((x for x in keep if len(tk & _toks(x["title"])) / max(1, len(tk | _toks(x["title"]))) >= 0.3
                    or (x["catalyst"] == g["catalyst"] and (set(x["who"]) & set(g["who"]) or len(tk & _toks(x["title"])) >= 2))), None)
        if dup:
            dup["also"] = dup.get("also", 0) + 1
            continue
        per[k] = per.get(k, 0) + 1
        keep.append(g)
        if len(keep) >= 10:
            break
    return {"items": keep, "scanned": len(raw), "errors": errs[:4]}


def run(force=False) -> dict | None:
    try:
        if not force and os.path.exists(OUT) and time.time() - os.path.getmtime(OUT) < EVERY:
            return None
    except Exception:
        pass
    now = datetime.now()
    try:
        ann = (json.load(open(N.OUT)).get("nse_announcements") or [])
    except Exception:
        ann = []
    res = {"at": now.strftime("%Y-%m-%d %H:%M"), "tabs": {}}
    for tab in FEEDS:
        try:
            res["tabs"][tab] = build_tab(tab, now, ann if tab == "india" else None)
        except Exception as e:
            res["tabs"][tab] = {"items": [], "error": str(e)[:120]}
    res["note"] = ("Only headlines that name something on the tab AND carry a price catalyst (results, orders, deals, ratings, "
                   "regulator action, guidance, ETF flows, hacks, OPEC / supply ...). Direction is read from the wording. Not a signal.")
    tmp = OUT + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(res, f, default=list)
    os.replace(tmp, OUT)
    return res


def api(tab: str | None = None) -> dict:
    try:
        d = json.load(open(OUT))
    except Exception:
        return {"tabs": {}, "error": "not built yet"}
    if tab:
        return {"at": d.get("at"), "note": d.get("note"), "tab": tab, **(d.get("tabs", {}).get(tab) or {"items": []})}
    return d


if __name__ == "__main__":
    r = run(force=True)
    for t, v in r["tabs"].items():
        print(f"== {t}: {len(v.get('items', []))} of {v.get('scanned')} {v.get('errors') or ''}")
        for g in v.get("items", []):
            print(f"  {'+' if g['dir'] > 0 else '-' if g['dir'] < 0 else '~'} {g['who'][0]:9s} {g['catalyst']:20s} {g['strength']:5} {g['title'][:90]}")
