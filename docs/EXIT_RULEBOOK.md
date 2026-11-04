# Exit rulebook – every paper rule (5 Nov 2026)

Paper only. Written from the running code. Each exit is the one the rule was tested with; changing one needs a re-test and the owner's approval.

| Rule | Stop | Target / profit booking | Trailing | Time exit |
|---|---|---|---|---|
| 4H break – ETH, SOL, other coins (`app/break4h.py`) | Block stop: back past the near edge (+0.04 %), any 4H high / low | No fixed target | From a 4H close ≥ entry + 1R: stop = best 4H close − 1.5 × ATR(14, 4H), only moves forward | Close of the 12th 4H bar (2 days) |
| 4H break – BTC | Block stop + 0.5 × ATR room | 3 targets 1R / 2R / 3R, a third each | Stop → breakeven after T1, → T1 after T2 | Rest at 2 days |
| 4H break – Gold (v1) | Block stop | None | None | 2 days |
| 4H break – Gold v2 (new 5 Nov, recorded beside v1) | Block stop | 3 targets 1R / 2R / 3R, a third each | Stop → breakeven after T1, → T1 after T2 | Rest at 2 days |
| Touch-minute option (`app/touchtrades.py`) | Spot 1-min close 0.10 % past the level (≥ 0.05 % from entry) | None – booked at the 30-min exit | +1R → breakeven; +2R → trail 1R behind the best 1-min close | 30 min or 15:20 |
| Touch + DOM v1 | Same as touch-minute | Same | Same | 30 min / 15:20 |
| DOM hold / break (`app/domcards.py`) | Beyond the level (hold: 2 bands past the far edge; break: 1 band inside), every second | Next level, at least 2R | None | 60 min or 15:20 flat |
| Strong Block Touch v2 (`app/strongblock.py`) | 1-min close 0.05 % beyond the block's far edge | 2R (1-min close) | +1R → breakeven | 60 min or 15:20 |
| Momentum basket (`app/xsecpaper.py`) | None | Rebuilt every 24 h (00:00 UTC) | None | 24 h |

Exit test 5 Nov (`scripts/research/b4h_exit_v4.py`): a breakeven floor on the coin trail was worse in both halves (ETH +1.04 % → +0.90 % net a trade), so the trail stays; Gold v2 targets +0.05 % vs v1 −0.08 % on 32 rebuilt trades (mixed halves). Known gaps: the 4H trail (1.5 × ATR) can sit below entry after +1R; touch-minute has no target; DOM has no breakeven step.
