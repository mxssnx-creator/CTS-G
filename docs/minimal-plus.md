# Minimal plus

Additional range. Off unless `grid.minimalPlus.enabled` is turned on. A preset of the same name is saved off as well.

Targets are 2× to 5× the position cost (0.20% round trip), in steps of 0.25. Stops are 0.5× to 3× the target, in steps of 0.25. A trailing cell uses a stop of at least 2.5× the target, higher than the short range. Indications in the test were Trend, Break, and RSI.

It does not run until two things are true: the switch is on, and at least one cell has been stored. Stored cells are the only ones built. Each of those configs is kept only when its previous closes are at least `lastN` (minimum 50, editable) and both that window and the whole tape clear `minPf` (minimum 1.20, default 1.35, above the usual 1.10). Its entry orders use their own tracking id, kind `M` (`CTSBV2_M…` on x02).

Test on 2026-09-30, 16 symbols, 8 days, 15-minute bars, 85 indications, 221 distinct cells: no cell cleared a full-sample profit factor of 1.35. The best full sample was about 0.73. A hot last-50 on the mixed book was not accepted. The range stays off.

Block knobs and their allowed ranges:

| Knob | Allowed | Tested best on this book | Live book |
|---|---|---|---|
| Levels | 1–10 | 4 | unchanged (10) |
| Volume ratio | 0.10–1 | 0.35 | unchanged (0.20) |
| Steps | 1–6 | 2 | 6 |
| Increase | 0.10–0.50 | 0.10 | 0.40 |
| Pause | 1–6 | 4 | 6 |

The tested Block replay went from PF 0.74 to 1.83 by skipping a bad window and sizing the better ones. That tune is on the Minimal plus preset, not on the live book.
