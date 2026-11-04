"""Shared exit simulator for the 4H break studies (copied from b4h_hybrid.py, 2026-11-05)."""
PLANS = {"TARGETS": ([(1, 1 / 3), (2, 1 / 3), (3, 1 / 3)], None), "H1": ([(1, 1 / 3), (2, 1 / 3)], "t1"),
         "H2": ([(1, 0.5)], "be"), "H3": ([(2, 0.5)], "be_after_1R")}


def sim(h, l, c, s, e, st, a, ver, bpb, n):
    R = abs(e - st); n = min(n, len(c)); cur, best, on, left, pnl = st, e, False, 1.0, 0.0
    if ver == "TRAIL":
        tg, floor = [], None
    else:
        tg, floor = PLANS[ver]
    hit = 0
    for k in range(n):
        if (s == 1 and l[k] <= cur) or (s == -1 and h[k] >= cur):
            return pnl + left * s * (cur - e)
        while hit < len(tg) and ((s == 1 and h[k] >= e + s * tg[hit][0] * R) or (s == -1 and l[k] <= e + s * tg[hit][0] * R)):
            px = e + s * tg[hit][0] * R; pnl += tg[hit][1] * s * (px - e); left -= tg[hit][1]; hit += 1
            if ver == "TARGETS" or ver == "H1":
                cur = e if hit == 1 else e + s * tg[hit - 2][0] * R
            elif ver == "H2":
                cur = e if s * (e - cur) > 0 else cur
        if left <= 1e-9:
            return pnl
        trailing = ver in ("TRAIL", "H3") or (ver == "H1" and hit >= 2) or (ver == "H2" and hit >= 1)
        if (k + 1) % bpb == 0:
            best = max(best, c[k]) if s == 1 else min(best, c[k])
            if ver in ("TRAIL", "H3") and s * (best - e) >= R: on = True
            if ver in ("H1", "H2") and trailing: on = True
            if on and trailing:
                nt = best - s * 1.5 * a
                cur = max(cur, nt) if s == 1 else min(cur, nt)
            if ver == "H3" and on:
                cur = max(cur, e) if s == 1 else min(cur, e)
            if ver == "H1" and hit >= 2:
                t1 = e + s * R; cur = max(cur, t1) if s == 1 else min(cur, t1)
    return pnl + left * s * (c[n - 1] - e)


