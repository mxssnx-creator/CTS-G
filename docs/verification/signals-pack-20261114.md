# Signals packs, distances and timeframes (2026-11-14)

Signals run as Set-engine packs `sig-<source>-<range>-t<tf>` (`server/pulse/signal_packs.py`): each its own Sets, stage chain, last-N windows, lanes and execution,
floor `minPfSignals` 2.2. Measured with `scripts/trade_sim.py` on 8 of the 40-symbol 3-day snapshot (BingX, 11-11 .. 11-14), only Signals packs on, every other x02
setting as shipped; development window first, the sealed window read once for the finalists and the shipped default.

| arm | development: executed PF / orders / net / hours positive | sealed |
|---|---|---|
| default: 5 / 15 min, TP steps 5-12, SL 0.5 / 0.75 / 1.0, base + trailing | 0.663 / 29 / -5.18 % / 5 of 12 | 0.650 / 32 / -5.06 % / 3 of 10 |
| TP steps 2-6 | 1.875 / 7 / +1.36 % / 2 of 5 | 0.178 / 19 / -4.70 % / 1 of 6 |
| TP steps 10-20 | 0.616 / 52 / -15.45 % | not read |
| trailing only, steps 6-15 | 0.863 / 43 / -2.43 % / 6 of 14 | **1.370 / 57 / +8.01 % / 7 of 12** |
| **1 / 5 min** | **0.909 / 88 / -3.76 % / 5 of 22** | **1.224 / 71 / +5.80 % / 6 of 13** |
| default + min SL distance 0.8 % | 0.502 / 41 / -12.49 % | not read |
| default + Exit Sets | 0.663 / 29 / -5.18 % (no Exit Set executed) | not read |

Both finalists beat the shipped default on both windows; x02 now runs the 1 / 5 min entry (more orders, better development figure). Neither is above 1 on the
development window, the samples are 43-88 orders per window, and the effective (selection) PF read 3.4-5.2 in every arm: this is a better arm, not proof of an edge.
Dev results were reproduced to the digit after the engine's memo change (the signals are bit-identical).

## 15 / 30 min (later the same day)

The 15 / 30 min packs read three days of 1m rows (`WINDOW_CAP_SLOW` 4,320; at 1,920 a 30 min pack had 64 bars, under two warm-ups of the 55-bar EMA). Every onset after one
window agrees with `signal_sources.onsets` on the full history (158 of 158, ETH, 3 days; 381 signals over 36 packs). Same 8 symbols and windows as above:

| arm (15 / 30 min) | development | sealed |
|---|---|---|
| steps 5-12, base + trailing | 0.852 / 69 / -4.28 % / 9 of 23 | 1.252 / 29 / +2.86 % / 8 of 12 |
| steps 8-20, base + trailing | 1.175 / 77 / +7.22 % / 9 of 24 | 1.032 / 35 / +0.60 % / 7 of 11 |
| **trailing only, steps 6-15** | **1.081 / 74 / +2.32 % / 12 of 24** | **1.332 / 33 / +3.14 % / 6 of 13** |

Trailing-only 15 / 30 min is above 1 on both windows with the higher minimum: x02 runs it as a second entry next to the 1 / 5 min one. 29-77 orders per window: a small sample.
