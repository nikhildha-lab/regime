"""Order-flow pattern explainers, one engine for every pattern (same look and voice as the Absorption sample).
python3 ofvid.py <key> stills | <key> | all        1280x720, 30 fps, narration by Kokoro (am_michael), PIL frames, ffmpeg per scene."""
import json, math, os, random, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, FPS = 1280, 720, 30
F = "/usr/share/fonts/opentype/inter/"
def font(w, s): return ImageFont.truetype(F + {"r": "Inter-Regular.otf", "m": "Inter-Medium.otf", "s": "Inter-SemiBold.otf", "b": "Inter-Bold.otf"}[w], s)
def MONO(s): return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", s)
BG, SURF, INK, MUTED, LINE = (236, 240, 243), (255, 255, 255), (22, 28, 36), (104, 112, 124), (218, 223, 230)
TEAL, BULL, BEAR, AMBER = (47, 93, 98), (23, 131, 74), (192, 57, 43), (176, 125, 16)
BULLS, BEARS = (213, 236, 221), (249, 226, 223)
COL = {"bull": BULL, "bear": BEAR, "amber": AMBER, "teal": TEAL}
TTS = "/tmp/claude-0/-home-claude/b55621cf-1e92-53c9-8f78-b52d3d1698bb/scratchpad/tts/"

def ease(x): x = max(0.0, min(1.0, x)); return x * x * (3 - 2 * x)
def mix(a, b, k): return tuple(int(a[i] + (b[i] - a[i]) * k) for i in range(3))
def card(d, x0, y0, x1, y1): d.rounded_rectangle((x0, y0, x1, y1), 18, fill=SURF, outline=LINE, width=2)
def pill(d, x, y, text, f, col, xmax=880, xmin=70):
    tw = d.textlength(text, font=f); x = max(xmin, min(x, xmax - tw - 8))
    d.rounded_rectangle((x - 4, y - 2, x + tw + 4, y + 17), 6, fill=SURF, outline=col, width=1)
    d.text((x, y), text, font=f, fill=col)


def wrap(d, text, f, w):
    out, cur = [], ""
    for wd in text.split():
        t = (cur + " " + wd).strip()
        if d.textlength(t, font=f) > w and cur: out.append(cur); cur = wd
        else: cur = t
    return out + [cur]


# ------------------------------------------------------------------ bars
def mk(lo, hi, o, c, skew=0.0, big=None, seed=None):
    rnd = random.Random(seed if seed is not None else lo * 100 + hi * 7 + o)
    rows = []
    for t in range(lo, hi + 1):
        s = rnd.randint(50, 160); b = rnd.randint(50, 160)
        if skew > 0: b = int(b * (1 + skew))
        if skew < 0: s = int(s * (1 - skew))
        rows.append([t, s, b])
    for t, s, b in (big or []):
        for r in rows:
            if r[0] == t: r[1], r[2] = s, b
    return {"lo": lo, "hi": hi, "o": o, "c": c, "rows": rows}


# ------------------------------------------------------------------ specs
SPECS = {}

SPECS["stack"] = dict(
    title="Stacked Imbalances", tagline="Buyers lift the offer again and again, at price after price.",
    base=2410.0, tick=0.5,
    concept=dict(left=("Aggressive Buyers", ["market orders that lift the offer", "→ 3+ prices in a row, buyers ≥ 3× sellers"], "bull"),
                 right=("Thin Offers", ["sellers step back", "→ price moves up fast through the rows"], "bear"),
                 arrow="bull",
                 lines=["Three or more imbalances stacked on top of each other mark a zone.",
                        "Buyers who started the move often defend it when price comes back.",
                        "The mirror case is a stack of sell imbalances - a zone above that sellers defend."]),
    bars=[mk(4, 8, 7, 5, -0.2), mk(4, 7, 5, 6, 0.1), mk(4, 8, 6, 6, 0),
          mk(6, 11, 6, 11, 0.3, big=[(6, 120, 180), (7, 62, 520), (8, 70, 610), (9, 48, 480), (10, 150, 210), (11, 95, 120)], seed=11),
          mk(10, 13, 11, 13, 0.3), mk(9, 12, 12, 10, -0.2), mk(9, 13, 10, 13, 0.4), mk(12, 15, 13, 15, 0.3)],
    key=3, key_rows=[7, 8, 9], key_col="bull",
    zone=dict(lo=7, hi=9, col="bull", label="stacked zone", from_bar=3, at="label"),
    build=[("Buy volume at a price is at least 3× the sell volume one row below (a diagonal imbalance)", "Look at the diagonal. At each price, buy volume is at least three times the sell volume one row below.", "bull"),
           ("Three rows in a row do it: a stack", "Here it happens on three rows in a row. That is a stack.", "bull"),
           ("The stack becomes a zone on the chart", "The stack becomes a zone on the chart.", "teal"),
           ("The bar closes near its high", "And the bar closes near its high.", "bull")],
    after=[("Price pulls back into the zone and buyers defend it", "Often, price pulls back into the zone, and the buyers who built it defend it.", "bull"),
           ("The move resumes from the zone", "The move then resumes from there.", "bull"),
           ("If a bar closes back below the whole zone, the buyers are gone - the idea is wrong", "But if a bar closes back below the whole zone, those buyers are gone, and the idea is wrong.", "bear")],
    rules=[("Imbalance", "buy at a price ≥ 3 × sell one row below (and ≥ 30 % of the bar's median row)"),
           ("Stack", "3 or more imbalanced rows in a row"),
           ("Zone", "from the lowest to the highest stacked row"),
           ("Model", "OF · Stacked Zone: buy the defended retest, stop beyond the zone"),
           ("Mirror", "stacked sell imbalances = a zone above that sellers defend (short)")],
    rule_spoken=["An imbalance is buy volume at least three times the sell volume one row below. Three or more in a row make a stack.",
                 "The stacked zone runs from the lowest to the highest of those rows. The paper model buys a defended retest, with the stop beyond the zone."],
    verdict=("On its own, a stack has not shown an edge after costs.", "One small pocket looks promising (crypto alts, 30-min). Live verdict: Research → Order Flow."),
    verdict_spoken="On its own, a stack has not shown an edge after costs. Only one small pocket looks promising so far. Use it as context, not a signal.")

SPECS["exhaust"] = dict(
    title="Exhaustion", tagline="Price makes a new high - and almost nobody trades up there.",
    base=2410.0, tick=0.5,
    concept=dict(left=("Late Buyers", ["the last few market orders", "→ push price to a new high"], "bull"),
                 right=("Nobody Follows", ["volume thins out row by row", "→ the push runs out of fuel"], "amber"),
                 arrow="amber",
                 lines=["A real breakout attracts more and more volume as it goes.",
                        "When volume shrinks into the new high, the buyers are exhausted.",
                        "The mirror case is volume thinning into a new low (a long)."]),
    bars=[mk(3, 7, 4, 7, 0.3), mk(5, 9, 6, 9, 0.3), mk(7, 11, 8, 11, 0.2),
          mk(9, 15, 10, 13, 0.1, big=[(13, 70, 85), (14, 30, 40), (15, 9, 14)], seed=21),
          mk(10, 13, 13, 10, -0.4), mk(8, 11, 10, 8, -0.4), mk(6, 9, 8, 6, -0.3), mk(5, 8, 6, 5, -0.2)],
    key=3, key_rows=[13, 14, 15], key_col="amber",
    line=dict(t=11, edge="top", label="prior highs", col="amber", bars=(0, 3)),
    build=[("The bar makes a new high above the last 3 bars", "First, the bar makes a new high, above the last three bars.", "amber"),
           ("Volume falls row by row into the top: 85, 40, 14", "Now read the top three rows. Volume falls, row by row, into the high.", "amber"),
           ("The top row trades less than a quarter of a normal row", "The very top row trades less than a quarter of a normal row.", "bear"),
           ("The bar closes off its high", "And the bar closes off its high.", "bear")],
    after=[("No one is left to buy higher: price rotates down", "With nobody left to buy higher, price often rotates back down.", "bear"),
           ("The thin top is often left behind", "The thin top is often left behind.", "bear"),
           ("If the next bars hold and push through the high with fresh volume, the move is not done - the idea is wrong", "But if the next bars push through the high with fresh volume, the move was not done, and the idea is wrong.", "bull")],
    rules=[("New extreme", "high above the highest high of the 3 bars before"),
           ("Thinning", "volume of the top 3 rows falls row by row toward the high"),
           ("Small top", "top row volume ≤ 25 % of the bar's median row"),
           ("Size", "the bar has at least 4 price rows"),
           ("Mirror", "the same at a new low = selling exhausted (long)")],
    rule_spoken=["The bar makes a high above the three bars before, and volume in the top three rows falls row by row toward the high.",
                 "The top row trades no more than a quarter of the bar's typical row. The mirror at a new low is a long."],
    verdict=("On its own, exhaustion has not shown an edge after costs.", "Mixed at best in a few groups. Live verdict: Research → Order Flow."),
    verdict_spoken="On its own, exhaustion has not shown an edge after costs. Treat it as a warning on a trade you already have.")

SPECS["unfinished"] = dict(
    title="Unfinished Auction", tagline="The high was still trading on both sides when price left it.",
    base=2410.0, tick=0.5,
    concept=dict(left=("Buyers", ["still lifting the offer", "at the very high"], "bull"),
                 right=("Sellers", ["still hitting the bid", "at the very high"], "bear"),
                 arrow="teal",
                 lines=["A finished auction ends with one side gone - zero trades on one side.",
                        "Here both sides were still trading at the extreme when price turned.",
                        "That price is unfinished business: the market often comes back to it."]),
    bars=[mk(3, 7, 4, 7, 0.2), mk(5, 9, 7, 8, 0.1), mk(6, 10, 8, 9, 0),
          mk(7, 13, 9, 11, 0.1, big=[(13, 210, 260), (12, 190, 230)], seed=31),
          mk(8, 11, 11, 9, -0.2), mk(7, 10, 9, 10, 0.1), mk(9, 12, 10, 12, 0.3), mk(11, 14, 12, 14, 0.3)],
    key=3, key_rows=[13], key_col="teal",
    line=dict(t=13, edge="top", label="unfinished high", col="teal", bars=(3, 7), at="build"),
    build=[("The bar makes a new high above the last 3 bars", "The bar makes a new high above the last three bars.", "teal"),
           ("The top row still trades both ways: 210 sold, 260 bought", "But look at the top row. It is still trading both ways - two hundred and ten sold, two hundred and sixty bought.", "teal"),
           ("Both sides at least a quarter of a normal row: the auction did not finish", "Both sides are at least a quarter of a normal row. The auction did not finish.", "teal"),
           ("That price is marked as a target until it is traded again", "So that price is marked as a target, until it is traded again.", "amber")],
    after=[("Price dips first, then returns to the unfinished high", "Often price dips first, and then returns to repair the unfinished high.", "bull"),
           ("Once traded again, the level is repaired and dropped", "Once that price trades again, the level is repaired, and it is dropped.", "teal"),
           ("It is a magnet, not a timing signal: price can take days to come back, or never", "It is a magnet, not a timing signal. Price can take days to come back, or never.", "bear")],
    rules=[("New extreme", "high above the highest high of the 3 bars before"),
           ("Both sides", "top row: buys AND sells each ≥ 25 % of the bar's median row"),
           ("Level", "that price stays a target until a later bar trades through it"),
           ("Size", "the bar has at least 4 price rows"),
           ("Mirror", "an unfinished low = a target below")],
    rule_spoken=["The bar makes a new high, and in its top row both buys and sells are at least a quarter of a normal row.",
                 "That price stays on the chart as a target until a later bar trades through it. The mirror is an unfinished low, a target below."],
    verdict=("Unfinished auctions have not paid as trades after costs.", "Use them as targets for trades you already have. Live verdict: Research → Order Flow."),
    verdict_spoken="As trades on their own, unfinished auctions have not paid after costs. Use them as targets, not entries.")

SPECS["finished"] = dict(
    title="Finished Auction", tagline="At the high, one side simply disappeared.",
    base=2410.0, tick=0.5,
    concept=dict(left=("Buyers", ["lift the last offer", "→ then stop completely"], "bull"),
                 right=("No Trade Above", ["zero buys at the top price", "→ the auction is complete"], "bear"),
                 arrow="bear",
                 lines=["An auction moves until one side stops trading.",
                        "A zero on one side at the extreme means the move found its end.",
                        "The mirror case is a finished low: zero sellers at the bottom (a long)."]),
    bars=[mk(3, 7, 4, 7, 0.2), mk(5, 9, 6, 9, 0.2), mk(7, 11, 8, 10, 0.1),
          mk(8, 14, 10, 11, 0, big=[(14, 46, 0), (13, 120, 70)], seed=41),
          mk(9, 12, 11, 9, -0.3), mk(7, 10, 9, 7, -0.3), mk(6, 9, 7, 7, -0.1), mk(5, 8, 7, 5, -0.2)],
    key=3, key_rows=[14], key_col="bear",
    line=dict(t=11, edge="top", label="prior highs", col="amber", bars=(0, 3)),
    build=[("The bar makes a new high above the last 3 bars", "The bar makes a new high above the last three bars.", "amber"),
           ("The top row: 46 sold, 0 bought", "Now the top row. Forty six contracts sold, and zero bought.", "bear"),
           ("One side at zero (or under 2 %): the auction is finished", "One side is at zero. The auction up there is finished.", "bear"),
           ("The bar closes back in its range", "And the bar closes back inside its range.", "bear")],
    after=[("With no buyers left at the top, price rotates down", "With no buyers left at the top, price often rotates back down.", "bear"),
           ("The finished high often holds as resistance", "The finished high often holds as resistance.", "bear"),
           ("If price later trades through it with volume, a new auction has started - the idea is wrong", "If price later trades through it with volume, a new auction has started, and the idea is wrong.", "bull")],
    rules=[("New extreme", "high above the highest high of the 3 bars before"),
           ("One side gone", "top row: the smaller side ≤ 2 % of the row (or zero)"),
           ("Size", "the bar has at least 4 price rows"),
           ("Side", "finished high = short; finished low = long"),
           ("Opposite", "both sides still trading = unfinished auction instead")],
    rule_spoken=["The bar makes a new high, and in its top row one side is zero, or under two percent of the row.",
                 "A finished high reads short, and a finished low reads long. If both sides were still trading, it would be an unfinished auction instead."],
    verdict=("On its own, a finished auction has not shown an edge after costs.", "It is common - so it is a weak signal alone. Live verdict: Research → Order Flow."),
    verdict_spoken="On its own, a finished auction has not shown an edge after costs. It is common, which makes it weak alone.")

SPECS["trapped"] = dict(
    title="Trapped Traders", tagline="Buyers chased the high - and price closed below where they bought.",
    base=2410.0, tick=0.5,
    concept=dict(left=("Breakout Buyers", ["chase the high with market orders", "→ buy imbalances at the top"], "bull"),
                 right=("Price Turns", ["closes below their buying", "→ every one of them is losing"], "bear"),
                 arrow="bear",
                 lines=["Trapped buyers have to sell to get out.",
                        "Their stops and exits add fuel to the move down.",
                        "The mirror case is trapped sellers at a low (a long)."]),
    bars=[mk(4, 8, 5, 8, 0.2), mk(6, 10, 8, 9, 0.1), mk(7, 11, 9, 10, 0.1),
          mk(8, 14, 10, 13, 0.2, big=[(12, 60, 290), (13, 55, 340), (14, 40, 260), (11, 110, 140)], seed=51),
          mk(9, 13, 13, 9, -0.6, seed=52), mk(7, 10, 9, 7, -0.4), mk(6, 9, 7, 6, -0.2), mk(5, 8, 6, 5, -0.2)],
    key=3, key_rows=[12, 13, 14], key_col="bull",
    zone=dict(lo=12, hi=14, col="bull", label="buyers' zone", from_bar=3, at="label"),
    mark=dict(bar=4, col="bear", label="close below the zone"),
    build=[("Buy imbalances in the top 3 rows: buyers chase the high", "Look at the top three rows. Buy imbalances - buyers chasing the high.", "bull"),
           ("Their buying marks a zone", "Their buying marks a zone.", "teal"),
           ("This bar or the next closes below the zone", "Now, this bar or the next one closes below that whole zone.", "bear"),
           ("Everyone who bought there is now trapped and losing", "Everyone who bought up there is now trapped, and losing.", "bear")],
    after=[("Trapped buyers sell to get out - the fall speeds up", "As trapped buyers sell to get out, the fall often speeds up.", "bear"),
           ("The zone above becomes resistance", "The zone above becomes resistance.", "bear"),
           ("If price closes back above the zone, the buyers are not trapped after all - the idea is wrong", "If price closes back above the zone, the buyers were not trapped after all, and the idea is wrong.", "bull")],
    rules=[("Imbalance", "a buy imbalance in the top 3 rows of a bar"),
           ("Zone", "from the lowest of those top 3 rows"),
           ("Trap", "the same bar or the next closed bar closes below the zone"),
           ("Side", "trapped buyers = short"),
           ("Mirror", "sell imbalance in the bottom 3 rows, then a close above = trapped sellers (long)")],
    rule_spoken=["A bar has a buy imbalance in its top three rows, and then the same bar, or the next one, closes below those rows.",
                 "That reads short. The mirror is a sell imbalance in the bottom three rows followed by a close above them - trapped sellers, a long."],
    verdict=("On its own, trapped traders have not shown an edge after costs.", "Close to break-even on stocks; mixed elsewhere. Live verdict: Research → Order Flow."),
    verdict_spoken="On its own, the trapped-trader pattern has not shown an edge after costs. On stocks it is close to break-even.")

SPECS["div"] = dict(
    title="Delta Divergence", tagline="Price makes a lower low - but the selling behind it is weaker.",
    base=2410.0, tick=0.5, cvd=True,
    concept=dict(left=("Price", ["makes a lower low", "→ looks like more selling"], "bear"),
                 right=("Cumulative Delta", ["makes a higher low", "→ there was less selling"], "bull"),
                 arrow="teal",
                 lines=["Cumulative delta adds up buys minus sells, bar after bar.",
                        "When price and delta disagree at a new low, sellers are losing steam.",
                        "The mirror case is a higher high on weaker buying (a short)."]),
    bars=[mk(9, 13, 13, 10, -0.4), mk(6, 10, 10, 7, -0.6), mk(5, 8, 7, 7, 0), mk(6, 10, 7, 9, 0.5),
          mk(6, 9, 9, 7, -0.1), mk(4, 7, 6, 6, 0.6), mk(6, 9, 6, 9, 0.6), mk(8, 12, 9, 12, 0.5)],
    key=5, key_rows=[4], key_col="teal",
    pivots=(2, 5),
    build=[("First low: price and cumulative delta both make a low", "Watch two lows. At the first low, price and cumulative delta both bottom.", "teal"),
           ("Second low: price goes lower", "At the second low, price goes lower.", "bear"),
           ("But cumulative delta makes a higher low: less selling", "But cumulative delta makes a higher low. There was less selling behind the move.", "bull"),
           ("Confirmed when 2 more bars do not break the new low", "It is confirmed once two more bars do not break the new low.", "teal")],
    after=[("Sellers are fading: price turns up from the second low", "Sellers are fading, and price often turns up from the second low.", "bull"),
           ("Best at yesterday's value edge (VAL / VAH)", "It works best when the low sits at yesterday's value area edge.", "teal"),
           ("If price breaks below the second low, the divergence failed - the idea is wrong", "If price breaks below the second low, the divergence failed, and the idea is wrong.", "bear")],
    rules=[("Pivots", "swing lows: lower than the 2 bars on each side"),
           ("Divergence", "second low below the first, cumulative delta higher than at the first"),
           ("Window", "the two lows at most 24 bars apart; confirmed 2 bars after"),
           ("Edge flag", "within 2 rows of yesterday's VAL (VAH for shorts)"),
           ("Mirror", "higher high with lower cumulative delta = short")],
    rule_spoken=["Two swing lows, each lower than the two bars on either side. The second is lower in price, but cumulative delta is higher.",
                 "The lows are at most twenty four bars apart, and the signal is confirmed two bars later. It is flagged when it sits at yesterday's value area edge."],
    verdict=("On its own, delta divergence has not shown an edge after costs.", "One small promising pocket (crypto alts, 30-min). Live verdict: Research → Order Flow."),
    verdict_spoken="On its own, delta divergence has not shown an edge after costs. One small pocket looks promising, and the live study keeps checking it.")


# ------------------------------------------------------------------ drawing
class V:
    def __init__(self, key):
        self.k = key; self.s = SPECS[key]; s = self.s
        self.B = s["bars"]
        self.D = [sum(r[2] - r[1] for r in b["rows"]) for b in self.B]
        c = 0; self.CVD = []
        for x in self.D: c += x; self.CVD.append(c)
        lo = min(b["lo"] for b in self.B) - 1; hi = max(b["hi"] for b in self.B) + 1
        self.T0, self.TN = lo, hi - lo + 1

    def header(self, d, sub):
        d.text((60, 40), "Order Flow Research", font=font("s", 22), fill=TEAL)
        d.text((60, 70), self.s["title"], font=font("b", 40), fill=INK)
        d.text((60, 122), sub, font=font("r", 20), fill=MUTED)

    def price(self, t): return self.s["base"] + t * self.s["tick"]

    def chart(self, img, n_bars, hi_k=0.0, label_k=0.0, mark_k=0.0, x0=60, y0=170, x1=900, y1=650):
        d = ImageDraw.Draw(img); s = self.s
        card(d, x0, y0, x1, y1)
        d.text((x0 + 24, y0 + 16), "Footprint · each row = sell volume × buy volume at that price", font=font("m", 16), fill=MUTED)
        top, bot = y0 + 50, y1 - 110
        rh = (bot - top) / self.TN; bw, gap = 92, 8; ax = x1 - 70
        Y = lambda t: bot - (t - self.T0 + 1) * rh             # top edge of row t
        for t in range(self.T0, self.T0 + self.TN):
            if t % 2 == 0:
                d.text((ax + 8, Y(t) + rh / 2 - 8), f"{self.price(t):.1f}", font=MONO(13), fill=MUTED)
        kc = COL[s["key_col"]]
        for i in range(min(n_bars, len(self.B))):
            b = self.B[i]; bx = x0 + 30 + i * (bw + gap); is_k = i == s["key"]
            for t, sv, bv in b["rows"]:
                y = Y(t)
                fill = BEARS if sv > bv * 1.25 else BULLS if bv > sv * 1.25 else (244, 246, 248)
                hot = is_k and hi_k > 0 and t in s["key_rows"]
                if hot: fill = mix(fill, mix(SURF, kc, 0.45), hi_k)
                d.rectangle((bx, y + 1, bx + bw, y + rh - 1), fill=fill)
                txt = f"{sv}×{bv}"; f = MONO(13 if rh >= 18 else 12)
                tw = d.textlength(txt, font=f)
                d.text((bx + (bw - tw) / 2, y + rh / 2 - 8), txt, font=f, fill=mix(INK, kc, hi_k) if hot else INK)
            oc_t = Y(max(b["o"], b["c"])); oc_b = Y(min(b["o"], b["c"])) + rh
            d.rectangle((bx + bw - 4, oc_t, bx + bw, oc_b), fill=BULL if b["c"] > b["o"] else BEAR)
            # delta / cumulative delta under the bar
            if s.get("cvd"):
                v = self.CVD[i]; lab = "CVD"
            else:
                v = self.D[i]; lab = "Δ"
            dcol = BULL if v > 0 else BEAR
            d.text((bx + 4, bot + 12), lab, font=font("m", 13), fill=MUTED)
            d.text((bx + (34 if lab == "CVD" else 22), bot + 11), f"{v:+d}", font=MONO(15), fill=dcol)
            if not s.get("cvd"):
                d.rectangle((bx + 6, bot + 40, bx + 6 + (bw - 12) * min(1, abs(v) / 1000), bot + 52), fill=mix((255, 255, 255), dcol, 0.75))
            if is_k and hi_k > 0 and not s.get("pivots"):
                o = int(4 + 4 * hi_k)
                d.rounded_rectangle((bx - o, Y(b["hi"]) - o, bx + bw + o, bot + 60), 10, outline=mix(SURF, kc, hi_k), width=3)
        # cumulative delta line + pivots (divergence)
        if s.get("cvd") and n_bars >= 2:
            vals = self.CVD[:n_bars]; mn, mxv = min(self.CVD), max(self.CVD); span = (mxv - mn) or 1
            pts = [(x0 + 30 + i * (bw + gap) + bw / 2, bot + 96 - (v - mn) / span * 52) for i, v in enumerate(vals)]
            d.line(pts, fill=TEAL, width=3)
            for p in pts: d.ellipse((p[0] - 3, p[1] - 3, p[0] + 3, p[1] + 3), fill=TEAL)
            a, b2 = s["pivots"]
            if n_bars > b2 and hi_k > 0:
                pa, pb = pts[a], pts[b2]
                d.line((pa, pb), fill=mix(SURF, BULL, hi_k), width=3)
                ya, yb = Y(self.B[a]["lo"]) + rh, Y(self.B[b2]["lo"]) + rh
                xa, xb = x0 + 30 + a * (bw + gap) + bw / 2, x0 + 30 + b2 * (bw + gap) + bw / 2
                d.line((xa, ya + 4, xb, yb + 4), fill=mix(SURF, BEAR, hi_k), width=3)
                pill(d, xb + 52, yb - 2, "lower low", font("s", 13), mix(SURF, BEAR, hi_k))
                pill(d, xb + 52, pb[1] - 10, "higher CVD low", font("s", 13), mix(SURF, BULL, hi_k))
        # horizontal reference line
        ln = s.get("line")
        if ln and label_k > 0 and n_bars > ln["bars"][0]:
            y = Y(ln["t"]) if ln["edge"] == "top" else Y(ln["t"]) + rh
            xa = x0 + 30 + ln["bars"][0] * (bw + gap); xb = x0 + 30 + min(ln["bars"][1], n_bars - 1) * (bw + gap) + bw
            c = mix(SURF, COL[ln["col"]], label_k)
            for xx in range(int(xa), int(xb), 12): d.line((xx, y, min(xx + 7, xb), y), fill=c, width=2)
            pill(d, xa + 4, y - 20, f"{ln['label']} {self.price(ln['t']):.1f}", font("m", 13), c)
        z = s.get("zone")
        if z and label_k > 0 and n_bars > z["from_bar"]:
            ya, yb = Y(z["hi"]), Y(z["lo"]) + rh
            xa = x0 + 30 + z["from_bar"] * (bw + gap) - 6; xb = x0 + 30 + (n_bars - 1) * (bw + gap) + bw + 6
            c = mix(SURF, COL[z["col"]], label_k)
            d.rectangle((xa, ya, xb, yb), outline=c, width=2)
            pill(d, xa + 2, ya - 21, f"{z['label']} {self.price(z['lo']):.1f}–{self.price(z['hi']):.1f}", font("m", 13), c)
        m = s.get("mark")
        if m and mark_k > 0 and n_bars > m["bar"]:
            b = self.B[m["bar"]]; bx = x0 + 30 + m["bar"] * (bw + gap); c = mix(SURF, COL[m["col"]], mark_k)
            d.rounded_rectangle((bx - 6, Y(b["hi"]) - 6, bx + bw + 6, Y(b["lo"]) + rh + 6), 10, outline=c, width=3)
            pill(d, bx - 4, Y(b["lo"]) + rh + 10, m["label"], font("s", 13), c)

    def side(self, img, items, title="What To See", x0=930, y0=170, x1=1220, y1=650):
        d = ImageDraw.Draw(img); card(d, x0, y0, x1, y1)
        d.text((x0 + 22, y0 + 18), title, font=font("s", 18), fill=TEAL)
        y = y0 + 58
        for text, k, col in items:
            if k <= 0: continue
            lines = wrap(d, text, font("m", 16), x1 - x0 - 50)
            d.ellipse((x0 + 22, y + 6, x0 + 32, y + 16), fill=mix(SURF, col, k))
            for l in lines:
                d.text((x0 + 42, y), l, font=font("m", 16), fill=mix(SURF, INK, k)); y += 22
            y += 12

    # ---- timings (original, before narration warp)
    def t_build(self): n0 = self.s["key"] + 1; return n0 * 1.0
    # ---- scenes
    def s_title(self, t, img):
        d = ImageDraw.Draw(img); k = ease(t / 1.2)
        d.text((100, 230), "Order Flow Patterns", font=font("s", 26), fill=mix(BG, TEAL, k))
        d.text((100, 270), self.s["title"], font=font("b", 84), fill=mix(BG, INK, k))
        d.text((100, 385), self.s["tagline"], font=font("r", 30), fill=mix(BG, MUTED, ease((t - 1.0) / 1.2)))
        d.text((100, 600), "Explainer · illustrative chart · research only, not advice", font=font("r", 18), fill=mix(BG, MUTED, ease((t - 2.4) / 1.0)))

    def s_concept(self, t, img):
        d = ImageDraw.Draw(img); c = self.s["concept"]
        self.header(d, "Who is on each side of the trade"); card(d, 60, 170, 1220, 650)
        k1, k2 = ease(t / 1.0), ease((t - 1.5) / 1.0)
        for (x, (ttl, ls, col), k) in ((120, c["left"], k1), (760, c["right"], k2)):
            cc = COL[col]; soft = BULLS if col == "bull" else BEARS if col == "bear" else (240, 236, 222) if col == "amber" else (222, 236, 236)
            d.rounded_rectangle((x, 260, x + 400, 420), 16, fill=mix(SURF, soft, k), outline=mix(SURF, cc, k), width=2)
            d.text((x + 30, 285), ttl, font=font("b", 28), fill=mix(SURF, cc, k))
            for j, l in enumerate(ls): d.text((x + 30, 330 + 30 * j), l, font=font("r", 20), fill=mix(SURF, INK, k))
        ac = COL[c["arrow"]]
        if t >= 1.5:
            for i in range(3):
                ph = (t * 1.4 + i / 3) % 1.0; x = 530 + ph * 210
                d.polygon([(x, 340), (x - 22, 328), (x - 22, 352)], fill=mix(SURF, ac, 0.9))
        d.line((530, 340, 750, 340), fill=mix(SURF, ac, k2 * 0.5), width=3)
        for j, (l, at) in enumerate(zip(c["lines"], (3.5, 5.5, 7.5))):
            k = ease((t - at) / 1.0)
            d.text((140, 470 + j * 50), l, font=font("s" if j == 0 else "r", 24 if j == 0 else 21), fill=mix(SURF, INK if j == 0 else MUTED, k))

    def s_build(self, t, img):
        d = ImageDraw.Draw(img); self.header(d, "How it looks on the footprint")
        tb = self.t_build(); n = min(self.s["key"] + 1, 1 + int(t / 1.0))
        self.chart(img, n, hi_k=ease((t - tb) / 0.8), label_k=ease((t - tb - 3.0) / 0.8), mark_k=ease((t - tb - 4.5) / 0.6))
        items = [(txt, ease((t - (tb + 1.5 * (j + 1))) / 0.6), COL[col]) for j, (txt, _, col) in enumerate(self.s["build"])]
        self.side(img, items)

    def s_after(self, t, img):
        d = ImageDraw.Draw(img); self.header(d, "What usually follows - and what can go wrong")
        k = self.s["key"]; n = k + 1 + min(len(self.B) - k - 1, int(t / 1.0) + 1) if t > 0.6 else k + 1
        self.chart(img, n, hi_k=1.0, label_k=1.0, mark_k=1.0)
        items = [(txt, ease((t - at) / 0.6), COL[col]) for (txt, _, col), at in zip(self.s["after"], (0.5, 2.5, 5.0))]
        self.side(img, items)

    def s_rule(self, t, img):
        d = ImageDraw.Draw(img); self.header(d, "Exactly how the engine detects it"); card(d, 60, 170, 1220, 650)
        y = 205
        for i, (a, b) in enumerate(self.s["rules"]):
            k = ease((t - 0.4 - i * 0.7) / 0.6)
            d.text((110, y), a, font=font("s", 22), fill=mix(SURF, TEAL, k))
            for j, l in enumerate(wrap(d, b, font("r", 20), 840)[:2]):
                d.text((300, y + 2 + j * 24), l, font=font("r", 20), fill=mix(SURF, INK, k))
            y += 60
        k = ease((t - 5.0) / 0.8)
        d.rounded_rectangle((100, 520, 1180, 620), 14, fill=mix(SURF, (250, 244, 226), k), outline=mix(SURF, AMBER, k), width=2)
        d.text((126, 538), self.s["verdict"][0], font=font("s", 22), fill=mix(SURF, INK, k))
        d.text((126, 574), self.s["verdict"][1], font=font("r", 19), fill=mix(SURF, MUTED, k))

    def scenes(self):
        tb = self.t_build()
        return [("01_title", 4.5, self.s_title), ("02_concept", 10.0, self.s_concept), ("03_build", tb + 1.5 * 4 + 2.0, self.s_build),
                ("04_after", 9.0, self.s_after), ("05_rule", 9.5, self.s_rule)]

    def cues(self):
        s = self.s; tb = self.t_build(); c = s["concept"]
        return {
            "01_title": [(0.3, s["title"] + "."), (1.0, s["tagline"])],
            "02_concept": [(0.2, f"{c['left'][0]}: {c['left'][1][0]}."), (1.5, f"{c['right'][0]}: {c['right'][1][0]}."),
                           (3.5, c["lines"][0]), (5.5, c["lines"][1]), (7.5, c["lines"][2])],
            "03_build": [(0.2, "Here is how it looks on the footprint. Each row shows sell volume against buy volume, at that price."), (tb, "Now watch this bar.")]
                        + [(tb + 1.5 * (j + 1), sp) for j, (_, sp, _) in enumerate(s["build"])],
            "04_after": [(at, sp) for (_, sp, _), at in zip(s["after"], (0.3, 2.5, 5.0))],
            "05_rule": [(0.3, "Here is the exact rule the engine uses."), (0.9, s["rule_spoken"][0]), (2.5, s["rule_spoken"][1]), (5.0, s["verdict_spoken"])],
        }


# ------------------------------------------------------------------ narration + render
def narrate(v, out):
    import soundfile as sf
    from kokoro_onnx import Kokoro
    kk = Kokoro(TTS + "kokoro.onnx", TTS + "voices.bin")
    ORIG = {n: s for n, s, _ in v.scenes()}; timing = {}
    for sc, cues in v.cues().items():
        clips = []
        for at, text in cues:
            a, sr = kk.create(text, voice="am_michael", speed=1.0, lang="en-us"); clips.append((at, a))
        starts, end, prev_a = [], 0.0, 0.0
        for i, (at, a) in enumerate(clips):
            st = at if i == 0 else max(starts[-1] + (at - prev_a), end + 0.35)
            starts.append(st); end = st + len(a) / sr; prev_a = at
        secs = round(max(starts[-1] + ORIG[sc] - clips[-1][0], end + 0.9), 2)
        buf = np.zeros(int(secs * sr) + sr, dtype=np.float32)
        for st, (at, a) in zip(starts, clips):
            i = int(st * sr); buf[i:i + len(a)] += a
        sf.write(os.path.join(out, f"vo_{sc}.wav"), buf[:int(secs * sr)], sr)
        timing[sc] = {"secs": secs, "keys": [[0, 0]] + [[a, round(x, 3)] for (a, _), x in zip(clips, starts)]}
    json.dump(timing, open(os.path.join(out, "timing.json"), "w"), indent=1)
    return timing


def warp(keys, t):
    for (a0, n0), (a1, n1) in zip(keys, keys[1:]):
        if t <= n1:
            return a0 + (t - n0) * (a1 - a0) / max(n1 - n0, 1e-6)
    a, n = keys[-1]
    return a + (t - n)


def render(v, out, timing):
    parts = []
    for name, secs, fn in v.scenes():
        tm = timing.get(name) or {"secs": secs, "keys": [[0, 0]]}
        secs = tm["secs"]; o = os.path.join(out, f"{name}.mp4"); tmp = o + ".part.mp4"
        p = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                              "-vf", f"fade=t=in:st=0:d=0.4,fade=t=out:st={secs - 0.4}:d=0.4", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", tmp],
                             stdin=subprocess.PIPE)
        for i in range(int(secs * FPS)):
            img = Image.new("RGB", (W, H), BG); fn(warp(tm["keys"], i / FPS), img); p.stdin.write(img.tobytes())
        p.stdin.close()
        if p.wait() == 0: os.replace(tmp, o)
        parts.append(o)
    open(os.path.join(out, "list.txt"), "w").write("".join(f"file '{x}'\n" for x in parts))
    open(os.path.join(out, "alist.txt"), "w").write("".join(f"file '{os.path.join(out, 'vo_' + n + '.wav')}'\n" for n, _, _ in v.scenes()))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(out, "list.txt"), "-c", "copy", os.path.join(out, "silent.mp4")], check=True)
    final = os.path.join(HERE, f"{v.k}.mp4"); tmp = final + ".part.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", os.path.join(out, "silent.mp4"), "-f", "concat", "-safe", "0", "-i", os.path.join(out, "alist.txt"),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-shortest", tmp], check=True)
    os.replace(tmp, final)
    return final


def stills(v, out):
    for name, secs, fn in v.scenes():
        img = Image.new("RGB", (W, H), BG); fn(secs * 0.97, img); img.save(os.path.join(out, f"still_{name}.png"))


if __name__ == "__main__":
    keys = list(SPECS) if sys.argv[1] == "all" else [sys.argv[1]]
    for key in keys:
        v = V(key); out = os.path.join(HERE, "work", key); os.makedirs(out, exist_ok=True)
        if len(sys.argv) > 2 and sys.argv[2] == "stills":
            stills(v, out); print("stills", key)
        else:
            tm = narrate(v, out); f = render(v, out, tm); print("done", key, f)
