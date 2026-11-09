import os
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

H = os.path.dirname(os.path.abspath(__file__))
pdfmetrics.registerFont(TTFont("DV", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
pdfmetrics.registerFont(TTFont("DVB", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
from reportlab.lib.fonts import addMapping
addMapping("DV", 0, 0, "DV"); addMapping("DV", 1, 0, "DVB")

D = pd.read_csv(os.path.join(H, "trades.csv")).sort_values(["day", "opened"])
U = D[~D.late_levels].copy()
LOT = {"NIFTY": 65, "BANKNIFTY": 30}
INK, MUT, ACC = colors.HexColor("#1b2430"), colors.HexColor("#5b6675"), colors.HexColor("#2457a6")
GRN, RED, LINE, BAND = colors.HexColor("#1f7a4a"), colors.HexColor("#b3261e"), colors.HexColor("#d6dbe2"), colors.HexColor("#f2f4f7")

ss = getSampleStyleSheet()
B = ParagraphStyle("b", parent=ss["Normal"], fontName="DV", fontSize=9.5, leading=13.5, textColor=INK, spaceAfter=4)
SM = ParagraphStyle("sm", parent=B, fontSize=8, leading=10.5, textColor=MUT)
H1 = ParagraphStyle("h1", parent=B, fontName="DVB", fontSize=15, leading=19, spaceBefore=6, spaceAfter=6, textColor=INK)
H2 = ParagraphStyle("h2", parent=B, fontName="DVB", fontSize=11.5, leading=15, spaceBefore=10, spaceAfter=4, textColor=ACC)
TI = ParagraphStyle("ti", parent=B, fontName="DVB", fontSize=22, leading=27, spaceAfter=4)
BL = ParagraphStyle("bl", parent=B, leftIndent=12, bulletIndent=2, spaceAfter=2)
CELL = ParagraphStyle("c", parent=B, fontSize=7.6, leading=9.4, spaceAfter=0)
CELLB = ParagraphStyle("cb", parent=CELL, fontName="DVB")


def rs(v):
    v = int(round(v)); return ("+" if v > 0 else "−" if v < 0 else "") + "₹" + f"{abs(v):,}"


def col(v):
    return GRN if v > 0 else RED if v < 0 else INK


def table(rows, widths, head=1, money_cols=(), zebra=True, font=7.6):
    data = [[Paragraph(str(c), CELLB if i < head else CELL) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=head)
    st = [("FONT", (0, 0), (-1, -1), "DV", font), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("LINEBELOW", (0, 0), (-1, head - 1), 0.8, INK), ("LINEBELOW", (0, head), (-1, -1), 0.3, LINE),
          ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5), ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if zebra:
        for i in range(head, len(rows)):
            if (i - head) % 2:
                st.append(("BACKGROUND", (0, i), (-1, i), BAND))
    t.setStyle(TableStyle(st))
    return t


def bullets(items):
    return [Paragraph(x, BL, bulletText="•") for x in items]


def money(v):
    c = "#1f7a4a" if v > 0 else "#b3261e" if v < 0 else "#1b2430"
    return f'<font color="{c}">{rs(v)}</font>'


# ---------- chart ----------
fig, ax = plt.subplots(figsize=(7.2, 2.3), dpi=200)
x = range(1, len(U) + 1)
for c, lab, clr in (("pnl_syn", "Synthetic future", "#2457a6"), ("pnl_opt", "0.6-delta option buy", "#c77d12"), ("pnl_fut", "Futures", "#6b7686")):
    ax.plot(x, U[c].cumsum().values, label=lab, color=clr, lw=2 if c == "pnl_syn" else 1.6, marker="o", ms=3)
ax.axhline(0, color="#9aa3ae", lw=0.8)
days = U.day.values
for i in range(1, len(days)):
    if days[i] != days[i - 1]:
        ax.axvline(i + 0.5, color="#d6dbe2", lw=0.8, ls="--")
pos = {}
for i, d in enumerate(days, 1):
    pos.setdefault(d, []).append(i)
for d, p in pos.items():
    ax.text(sum(p) / len(p), ax.get_ylim()[1] * 0.97, pd.Timestamp(d).strftime("%a %d %b"), ha="center", fontsize=7, color="#5b6675")
ax.set_xlabel("trade number (in time order)", fontsize=8); ax.set_ylabel("running P&L, ₹ (1 lot)", fontsize=8)
ax.tick_params(labelsize=7); ax.legend(fontsize=7, frameon=False, loc="lower right")
for s in ("top", "right"):
    ax.spines[s].set_visible(False)
ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v/1000:+.0f}k" if v else "0"))
plt.tight_layout(); plt.savefig(os.path.join(H, "equity.png")); plt.close()

# ---------- stats ----------
def stats(c, d=U):
    s = d[c]; eq = s.cumsum(); w = s[s > 0].sum(); l = -s[s < 0].sum()
    return {"tot": s.sum(), "avg": s.mean(), "win": (s > 0).mean() * 100, "pf": w / l if l else float("nan"), "dd": (eq - eq.cummax()).min(),
            "best": s.max(), "worst": s.min(), "ex6": s[d.day != "2026-11-06"].sum(), "x3": s.sort_values().iloc[:-3].sum()}


S = {c: stats(c) for c in ("pnl_fut", "pnl_syn", "pnl_opt")}
P = U[U.plan]
SP = {c: stats(c, P) for c in ("pnl_fut", "pnl_syn", "pnl_opt")}
lotm = U.key.map(LOT)
syn2 = ((U.pts - 2 * U.key.map({"NIFTY": 4.5, "BANKNIFTY": 12.5})) * lotm).sum()
fut2 = ((U.pts - 2 * U.fut_cost_pts) * lotm).sum()
opt_prem = (U.opt_in * lotm)

story = []
story += [Paragraph("DOM Index — Strategy Logic &amp; Paper Results", TI),
          Paragraph("Nifty and Bank Nifty intraday · order-book (DOM) confirmation at pre-marked levels · paper trades 4–9 Nov 2026 · prepared 9 Nov 2026", SM),
          Spacer(1, 6)]
kp = [["", "Futures", "Synthetic future", "0.6-delta option buy"],
      ["Net P&amp;L, 17 trades, 1 lot", money(S["pnl_fut"]["tot"]), money(S["pnl_syn"]["tot"]), money(S["pnl_opt"]["tot"])],
      ["Win rate", f'{S["pnl_fut"]["win"]:.0f}%', f'{S["pnl_syn"]["win"]:.0f}%', f'{S["pnl_opt"]["win"]:.0f}%'],
      ["Without Fri 6 Nov", money(S["pnl_fut"]["ex6"]), money(S["pnl_syn"]["ex6"]), money(S["pnl_opt"]["ex6"])]]
story.append(table(kp, [62 * mm, 37 * mm, 37 * mm, 40 * mm], zebra=False, font=9))
story.append(Spacer(1, 6))
story.append(Paragraph(
    "<b>Short answer.</b> The signal made money in paper trading (+9.4 R gross over 17 trades), but almost all of it came from one session "
    "(Fri 6 Nov). The <b>synthetic future</b> is the best way to trade it — same move as futures at about a quarter of the cost. "
    "Plain futures lose a large share to STT. The 0.6-delta option keeps about 60% of the move with a much smaller capital outlay, but "
    "it never beats the synthetic in rupees per lot. Four sessions is far too few to call this an edge: keep paper trading to 30–40 trades.", B))

story.append(Paragraph("1. What the strategy does", H1))
story.append(Paragraph(
    "Each morning the engine marks a handful of <b>price levels</b> where big players are likely to act. During the day it watches the live "
    "<b>order book</b> (20 levels of bids and offers, every second) and the <b>tape</b> (market buys vs market sells) of the near-month Nifty and "
    "Bank Nifty futures. A trade is taken only when price reaches a level <i>and</i> the order book confirms what price is doing there. "
    "There are two setups: <b>HOLD</b> (the level holds and price bounces) and <b>BREAK</b> (the level gives way and price runs through).", B))

story.append(Paragraph("1.1 Levels (fixed for the day)", H2))
story += bullets([
    "<b>Level-map blocks</b> (medium and strong) from the Level Map, moved from spot to futures by that day's basis (futures − spot).",
    "<b>Option walls</b>: the strikes with the biggest call OI (resistance) and put OI (support), each turned into a zone ± 0.04%.",
    "<b>Initial balance</b>: the 09:15–10:15 high and low, added once 10:15 has passed.",
    "Each level is a zone (low–high), not a single price. One <i>band</i> = the width of that zone; stops are measured in bands."])

story.append(Paragraph("1.2 Measures used", H2))
story += bullets([
    "<b>Book lean</b>: the 20-level bid/offer imbalance, averaged over 30 seconds, minus its normal (30-minute median). Positive = more buyers waiting than usual.",
    "<b>Delta</b>: market buys − market sells. <b>Absorption</b>: aggressive selling hits the zone but price doesn't fall — someone passive is soaking it up."])

story.append(Paragraph("2. Entry rules — step by step", H1))
story.append(Paragraph("2.1 HOLD (bounce from the level)", H2))
hold = [["Step", "Long (at support)", "Short (at resistance)"],
        ["1. Fresh touch", "Price enters the zone after being away from it for 120 s or more", "Same"],
        ["2. No real break", "Price does not go more than 2 bands below the zone's low", "Not more than 2 bands above the zone's high"],
        ["3. Rejection", "Within 120 s price leaves the zone upwards (back on the side it came from)", "Leaves downwards"],
        ["4. Book confirms", "Book lean ≥ +0.10 (buyers stacking)", "Book lean ≤ −0.10 (sellers stacking)"],
        ["5. Absorption", "While in the zone, market sells ≥ market buys (sellers absorbed)", "Market buys ≥ market sells (buyers absorbed)"],
        ["Entry", "Buy at the mid price when all 5 are true", "Sell at the mid"]]
story.append(table(hold, [30 * mm, 75 * mm, 71 * mm]))
story.append(Paragraph("2.2 BREAK (run through the level)", H2))
brk = [["Step", "Long (break up)", "Short (break down)"],
       ["1. Exit far side", "Price goes through the zone and out of its top edge", "Out of the bottom edge"],
       ["2. Acceptance", "Stays above the edge for 30 s", "Stays below for 30 s"],
       ["3. Book confirms", "Book lean ≥ +0.15", "Book lean ≤ −0.15"],
       ["4. Aggression", "Delta over the last 60 s ≥ 15% of the volume, buy side", "≥ 15% of volume, sell side"],
       ["Entry", "Buy at the mid", "Sell at the mid"]]
story.append(table(brk, [30 * mm, 75 * mm, 71 * mm]))
story.append(Paragraph("2.3 Filters that apply to both", H2))
story += bullets(["New trades only between 09:30 and 15:00.",
                  "One open trade per index at a time; max 4 trades per index per day; each level / setup / side fires only once a day.",
                  "<b>Plan account</b> (the version shown in Paper Trades): only the first 3 index trades of the day, and stop for the day once down 2 R."])

story.append(Paragraph("3. Exit rules — stop, target, time, trail", H1))
ex = [["", "HOLD", "BREAK"],
      ["Stop-loss (SL)", "2 bands beyond the far edge of the zone (the level is wrong if price gets there)", "1 band back inside the zone (the break failed)"],
      ["Risk (1 R)", "Entry − stop. Typical: Nifty 40–60 pts, Bank Nifty 90–200 pts", "Same idea; typical Nifty ~37, Bank Nifty ~127 pts"],
      ["Target", "The next level that is at least 2 R away; if none, exactly 2 R", "Same"],
      ["Time exit", "Out at market after 60 minutes if neither stop nor target hit", "Same"],
      ["End of day", "Everything flat at 15:20", "Same"],
      ["Trailing SL", "None in the live rule today (fixed stop). Trail options tested below as what-ifs", "None"],
      ["Checked on", "The futures mid price, every second", "Same"]]
story.append(table(ex, [30 * mm, 80 * mm, 66 * mm]))
story.append(Spacer(1, 4))
story.append(Paragraph(
    "<b>Trailing stop — what-if test.</b> Replayed on the real per-second futures path for all 17 trades (synthetic-future costs). "
    "The replay of the live rule matched the recorded exit on all 17 trades, so the data is consistent.", B))
tr = [["Version", "Rule", "Net P&amp;L", "Avg R", "Win %"]]
nm = {"BASE": ("Live rule", "Fixed stop, target ≥ 2 R, 60-min exit"), "BE": ("Breakeven", "At +1 R, stop moves to entry"),
      "TRAIL1R": ("Trail 1 R", "At +1 R, stop trails 1 R behind the best price; target kept"),
      "RUNNER": ("Runner", "At +1 R, trail 1 R; no target; still 60-min exit")}
for m, (a, b) in nm.items():
    s = U[f"pnl_{m}"]; r = (U[f"pts_{m}"] / U.risk_pts).mean()
    tr.append([a, b, money(s.sum()), f"{r:+.2f}", f"{(s > 0).mean()*100:.0f}%"])
story.append(table(tr, [24 * mm, 88 * mm, 26 * mm, 18 * mm, 20 * mm]))
story.append(Paragraph(
    "None of the trails beats the fixed stop. Trades either hit the stop fast (6 stops, most within 1–10 minutes, before ever reaching +1 R) or "
    "ran cleanly to target / time. Breakeven changed one trade; Trail 1 R saved one flat trade but cut the biggest winner early (6 Nov Bank Nifty 10:20: +316 pts → +187); "
    "the Runner helped two late-day trades but lost on others. <b>Keep the fixed stop.</b> Revisit after 40+ trades.", B))

story.append(PageBreak())
story.append(Paragraph("4. Which instrument", H1))
inst = [["", "Futures", "Synthetic future", "0.6-delta option buy"],
        ["What you do", "Buy / sell 1 lot of the near-month future", "Long: buy ATM call + sell ATM put (short: the reverse), same expiry",
         "Long: buy the call with delta 0.6; short: buy the put with delta −0.6. Expiry: first one 5+ days away"],
        ["Moves with index", "1 : 1", "≈ 1 : 1", "≈ 0.6 : 1 at entry (changes as price moves)"],
        ["Cost per round trip", "~16.5 pts Nifty · ~40 pts Bank Nifty (STT 0.05% on the sell side is most of it)",
         "4.5 pts Nifty · 12.5 pts Bank Nifty (STT only on option premium)", "₹25 + 0.10% of premium a side + 0.5 / 1.5 pt spread a side"],
        ["Money blocked", "Full futures margin", "About the same margin (short put)", f"Only the premium: median ₹{opt_prem.median():,.0f} per lot here"],
        ["Max loss on a gap", "Open-ended", "Open-ended", "Limited to premium paid"],
        ["Time decay", "None", "None (call and put cancel)", "Yes — small for a 1-hour hold with 5+ days left"]]
story.append(table(inst, [27 * mm, 47 * mm, 50 * mm, 52 * mm]))
story.append(Paragraph(
    "How the option numbers were made: for each trade the strike with delta 0.6 was found at entry, then priced with Black-Scholes at entry and exit using "
    "the index level (futures − basis) and the ATM implied volatility from the 3-minute option-chain snapshot nearest each time. So time decay and IV changes are "
    "included. These are modelled prices, not real fills — real fills could be a little worse.", SM))

story.append(Paragraph("5. P&amp;L comparison (1 lot, after costs)", H1))
cmp = [["", "Futures", "Synthetic future", "0.6-delta option buy"]]
for lab, k, f in (("Net P&amp;L", "tot", money), ("Average per trade", "avg", money), ("Win rate", "win", lambda v: f"{v:.0f}%"),
                  ("Profit factor", "pf", lambda v: f"{v:.2f}"), ("Max drawdown", "dd", money), ("Best trade", "best", money), ("Worst trade", "worst", money),
                  ("Without Fri 6 Nov", "ex6", money), ("Without the best 3 trades", "x3", money)):
    cmp.append([lab] + [f(S[c][k]) for c in ("pnl_fut", "pnl_syn", "pnl_opt")])
cmp.append(["Costs doubled", money(fut2), money(syn2), "—"])
cmp.append([f"Plan account only ({len(P)} trades)", money(SP["pnl_fut"]["tot"]), money(SP["pnl_syn"]["tot"]), money(SP["pnl_opt"]["tot"])])
story.append(table(cmp, [50 * mm, 40 * mm, 42 * mm, 44 * mm]))
story.append(Spacer(1, 6))
story.append(Image(os.path.join(H, "equity.png"), width=176 * mm, height=176 * mm * 2.3 / 7.2))
story.append(Paragraph(
    f"Gross, before any costs, the signal made {U.pts.mul(lotm).sum():,.0f} rupees per lot ({U.r_gross.sum():+.1f} R). "
    "Futures give up nearly 60% of that to costs; the synthetic gives up under 20%. The option keeps roughly 0.6 of each move on both winners and losers, "
    "so it smooths the curve (smallest drawdown) but earns less.", B))
by = [["Split", "Trades", "Futures", "Synthetic", "Option"]]
for lab, m in (("Nifty", U.key == "NIFTY"), ("Bank Nifty", U.key == "BANKNIFTY"), ("HOLD setup", U.setup == "hold"), ("BREAK setup", U.setup == "break")):
    by.append([lab, int(m.sum())] + [money(U[m][c].sum()) for c in ("pnl_fut", "pnl_syn", "pnl_opt")])
story.append(table(by, [40 * mm, 20 * mm, 38 * mm, 38 * mm, 40 * mm]))

story.append(PageBreak())
story.append(Paragraph("6. Every trade (3–9 Nov)", H1))
story.append(Paragraph(
    "Prices are the near-month futures mid. P&amp;L is for 1 lot (Nifty 65, Bank Nifty 30) after costs. The six 3 Nov trades are shown greyed: the morning levels "
    "were saved late that day, so they could have used information not available at 09:15 — they are kept out of every total, as in Paper Trades. "
    "* = counted in the plan account. BNF = Bank Nifty. Option column shows the modelled strike and premium in → out. Sun 8 Nov had no session. One Nifty short from 11:12 on 9 Nov was still open and is not included.", SM))
rows = [["Day", "Idx", "Setup", "Level", "Time in–out", "Entry", "Exit", "Out by", "Pts", "R", "Futures", "Synth.", "0.6Δ option"]]
for _, r in D.iterrows():
    mo = (lambda v: rs(v)) if r.late_levels else money
    row_ = [pd.Timestamp(r.day).strftime("%a %d") + (" *" if r.plan else ""), "Nifty" if r.key == "NIFTY" else "BNF", f"{r.setup.upper()}<br/>{r.side}", str(r.level).replace("call wall 58000 + put wall 58000", "call + put wall 58000")[:28],
                 f"{r.opened}–{r.closed}", f"{r.entry:.0f}", f"{r.exit:.0f}", r.exit_reason, f"{r.pts:+.0f}", f"{r.r_gross:+.1f}",
                 mo(r.pnl_fut), mo(r.pnl_syn), mo(r.pnl_opt) + f'<br/><font size="5.6" color="#5b6675">{int(r.opt_strike)} {r.opt_cp} {r.opt_in:.0f}→{r.opt_out:.0f}</font>']
    if r.late_levels:
        row_ = [f'<font color="#9aa3ae">{c}</font>' for c in row_]
    rows.append(row_)
t = table(rows, [13 * mm, 10 * mm, 12 * mm, 21 * mm, 19 * mm, 11 * mm, 11 * mm, 11 * mm, 10 * mm, 9 * mm, 15 * mm, 16 * mm, 18 * mm], font=6.6)
extra = [("FONT", (0, 0), (-1, -1), "DV", 6.6), ("LEFTPADDING", (0, 0), (-1, -1), 1.5), ("RIGHTPADDING", (0, 0), (-1, -1), 1.5)]
for i, (_, r) in enumerate(D.iterrows(), 1):
    if r.late_levels:
        extra.append(("TEXTCOLOR", (0, i), (-1, i), colors.HexColor("#9aa3ae")))
t.setStyle(TableStyle(extra))
GREY = ParagraphStyle("tg", parent=CELL, fontSize=6.6, leading=8.2, textColor=colors.HexColor("#9aa3ae"))
NORM = ParagraphStyle("t", parent=CELL, fontSize=6.6, leading=8.2)
for (_, r), row in zip(D.iterrows(), t._cellvalues[1:]):
    for c in row:
        c.style = GREY if r.late_levels else NORM
story.append(t)
story.append(Spacer(1, 4))
dd = [["Day", "Trades", "Futures", "Synthetic", "0.6Δ option"]]
for d, g in U.groupby("day"):
    dd.append([pd.Timestamp(d).strftime("%a %d %b"), len(g)] + [money(g[c].sum()) for c in ("pnl_fut", "pnl_syn", "pnl_opt")])
dd.append(["Total", len(U)] + [money(U[c].sum()) for c in ("pnl_fut", "pnl_syn", "pnl_opt")])
story.append(KeepTogether([Paragraph("Day by day", H2), table(dd, [40 * mm, 20 * mm, 38 * mm, 38 * mm, 40 * mm])]))

story.append(Paragraph("7. Conclusion on performance", H1))
story += bullets([
    f"<b>The signal is positive so far but not proven.</b> 17 clean trades over 4 sessions, {U.r_gross.sum():+.1f} R gross, 9 winners. "
    "One session (Fri 6 Nov) made more than the total; take it out and every instrument is flat to negative. That is normal for a 2 R-target system, "
    "but it means the sample can't tell skill from one good day.",
    "<b>HOLD carries the results; BREAK has only 2 trades</b> (one target on 6 Nov, one fast stop on 9 Nov) — nothing can be said about it yet.",
    "<b>Stops are fast.</b> All 6 stops hit within ~20 minutes, most within 10. Winners mostly ran to the 60-minute time exit or the target.",
    "<b>Instrument: use the synthetic future.</b> It keeps over 80% of the gross move. Futures keep only about 40% because of STT on the full contract value, "
    "and with doubled costs futures turn negative while the synthetic stays clearly positive.",
    "<b>The 0.6-delta option is the choice if capital or gap risk matters more than rupees:</b> a small fraction of the futures margin, loss capped at the premium, "
    "smallest drawdown — but about 40% less profit than the synthetic in this sample.",
    "<b>Trailing stop: don't add one yet.</b> Breakeven, 1 R trail and runner versions all did the same or worse than the fixed stop.",
    "<b>Plan account</b> (first 3 a day, −2 R daily stop) made less (synthetic "
    f"{rs(SP['pnl_syn']['tot'])} on {len(P)} trades) because the best 6 Nov trades came after the first three.",
    "<b>Next step:</b> no rule changes. Keep paper trading; judge again at 30–40 trades (about 2–3 more weeks), checking net R per trade, HOLD vs BREAK, "
    "and whether results still depend on one day."])
story.append(Spacer(1, 8))
story.append(Paragraph(
    "Paper results and modelled option prices only — no live orders. Futures cost assumes STT 0.05% on the sell side, exchange / SEBI / stamp / GST, "
    "₹40 brokerage and 1 pt (Nifty) / 3 pts (Bank Nifty) slippage a side. Synthetic cost is the app's fixed 4.5 / 12.5 pts. Not financial advice. "
    "Data: data_store/livedesk/domcards/trades.json, per-second order-flow files, 3-minute option-chain snapshots. Script: scripts/research/dom_ix_report_data.py.", SM))


def foot(c, d):
    c.saveState(); c.setFont("DV", 7); c.setFillColor(MUT)
    c.drawString(18 * mm, 10 * mm, "DOM Index · logic and paper results · 9 Nov 2026"); c.drawRightString(192 * mm, 10 * mm, f"page {d.page}"); c.restoreState()


out = "/mnt/user-data/outputs/DOM_Index_Strategy_Report.pdf"
doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm, topMargin=15 * mm, bottomMargin=16 * mm,
                        title="DOM Index — Strategy Logic & Paper Results", author="Claude for Nikhil")
doc.build(story, onFirstPage=foot, onLaterPages=foot)
print(out)
