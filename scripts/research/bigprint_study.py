"""Big-print study, Nifty / Bank Nifty futures (Dhan order-flow tape).

Question: after an unusually large classified trade, does price move in that trade's direction?
Rules (MASTER_BACKTESTER): no look-ahead - the "big" threshold at any moment uses only the
PREVIOUS 500 trades; both halves of the sample reported; costs charged; a control of random
minutes for the same horizons.
"""
import csv, gzip, glob, os, statistics as st, random
random.seed(7)

LOT = {"NIFTY": 65, "BANKNIFTY": 30}
COST = {"NIFTY": 4.5, "BANKNIFTY": 12.5}     # round-trip points, same as the level studies
HOR = [5, 15, 30]                            # minutes
WARM = 500                                   # trades used to set the threshold
MULT = [8, 15, 30]                           # big = this many times the rolling median size

def load(sym):
    days = {}
    for p in sorted(glob.glob(f"/mnt/user-data/uploads/Documents/dhan/orderflow/data/of/*/trades_{sym}.csv.gz")):
        day = p.split("/")[-2]
        if os.path.getsize(p) < 500: continue          # empty / still-opening file
        rows, bad = [], 0
        try:
            with gzip.open(p, "rt") as f:
                for r in csv.reader(f):
                    if len(r) != 4: continue
                    try: rows.append((float(r[0]), float(r[1]), int(r[2]), int(r[3])))
                    except ValueError: continue
        except Exception as e:                           # a half-written gzip member: keep what we read
            bad = 1
        if bad: print(f"   (note: {day} {sym} tape truncated - using the {len(rows):,} rows that read cleanly)")
        # sanity: drop corrupt rows (a truncated gzip member can yield junk numbers)
        if rows:
            med = st.median([r[1] for r in rows])
            rows = [r for r in rows if 0.9 * med < r[1] < 1.1 * med and 0 < r[2] < 10_000_000
                    and 1_600_000_000 < r[0] < 2_000_000_000]
            rows.sort(key=lambda r: r[0])
        if len(rows) > WARM + 200: days[day] = rows
    return days

def fwd(rows, i, mins):
    """last price at least `mins` after rows[i]; None if the session ends first"""
    t_end = rows[i][0] + mins * 60
    j = i
    while j < len(rows) - 1 and rows[j][0] < t_end: j += 1
    return rows[j][1] if rows[j][0] >= t_end else None

def run(sym):
    days = load(sym)
    print(f"\n=== {sym}: {len(days)} sessions, {sum(len(v) for v in days.values()):,} trades")
    out = {}
    for mult in MULT:
        ev = {h: [] for h in HOR}            # signed move in points, in the print's direction
        ctl = {h: [] for h in HOR}
        nbig = 0
        for day, rows in sorted(days.items()):
            sizes = [r[2] for r in rows]
            for i in range(WARM, len(rows)):
                med = st.median(sizes[i-WARM:i])          # only past trades
                t, px, q, side = rows[i]
                if q < med * mult: continue
                nbig += 1
                for h in HOR:
                    f = fwd(rows, i, h)
                    if f is not None: ev[h].append(side * (f - px))
            # control: the same horizons from random trades in the same session
            for _ in range(60):
                i = random.randrange(WARM, len(rows) - 1)
                side = random.choice([1, -1])
                for h in HOR:
                    f = fwd(rows, i, h)
                    if f is not None: ctl[h].append(side * (f - rows[i][1]))
        print(f"\n-- big = {mult}x the rolling median trade ({nbig} prints, "
              f"{nbig/max(1,len(days)):.0f} per session)")
        for h in HOR:
            e, c = ev[h], ctl[h]
            if len(e) < 30:
                print(f"   {h:>2} min: only {len(e)} samples - not enough"); continue
            m, mc = st.mean(e), st.mean(c)
            sd = st.pstdev(e) or 1e-9
            t_stat = m / (sd / len(e) ** 0.5)
            win = sum(1 for v in e if v > 0) / len(e) * 100
            net = m - COST[sym]
            half = len(e) // 2
            h1, h2 = st.mean(e[:half]), st.mean(e[half:])
            print(f"   {h:>2} min: n={len(e):<5} mean {m:+.2f} pts (control {mc:+.2f}) · "
                  f"net of cost {net:+.2f} · right {win:.0f}% · t {t_stat:+.2f} · halves {h1:+.2f} / {h2:+.2f}")
        out[mult] = ev
    return out

for s in ("NIFTY", "BANKNIFTY"):
    run(s)
