"""Fetch the longest 15-min history Hyperliquid serves (5000 candles, ~52 days) for the xyz perps of the Gold & Silver and Forex
trackers, in the replay format (datasets/hl-xyz-tf15/year/<KEY>_perp.csv). Public data, no keys. 2026-10-23."""
import os, time, csv, requests
DOC = os.path.expanduser("~/Documents")
SETS = {"metals-tracker": {"GOLD": "xyz:GOLD", "SILVER": "xyz:SILVER", "COPPER": "xyz:COPPER", "PAXG": "PAXG"},
        "fx-tracker": {"EURUSD": "xyz:EUR", "USDJPY": "xyz:JPY", "GBPUSD": "xyz:GBP"}}
URL = "https://api.hyperliquid.xyz/info"
end = int(time.time() * 1000); start = end - 5000 * 15 * 60 * 1000
for tracker, coins in SETS.items():
    out = os.path.join(DOC, tracker, "datasets", "hl-xyz-tf15", "year"); os.makedirs(out, exist_ok=True)
    for key, coin in coins.items():
        rows, t0 = [], start
        while t0 < end:
            r = requests.post(URL, json={"type": "candleSnapshot", "req": {"coin": coin, "interval": "15m", "startTime": t0, "endTime": end}}, timeout=30).json()
            if not r: break
            rows += r; nt = int(r[-1]["t"]) + 1
            if nt <= t0 or len(r) < 2: break
            t0 = nt; time.sleep(0.3)
        seen, n = set(), 0
        with open(os.path.join(out, f"{key}_perp.csv"), "w", newline="") as f:
            w = csv.writer(f); w.writerow(["datetime", "open", "high", "low", "close", "volume", "oi"])
            for k in sorted(rows, key=lambda x: x["t"]):
                if k["t"] in seen: continue
                seen.add(k["t"]); n += 1
                w.writerow([time.strftime("%Y-%m-%d %H:%M", time.gmtime(k["t"] / 1000)), k["o"], k["h"], k["l"], k["c"], k["v"], 0])
        print(tracker, key, n, "candles")
