# Successful configs

Wide trailing only. Read from the x02 paper book on 2026-09-30, after undoing the timeframe-lane scale. A pair is in this set when it is not a signal, has at least 12 closes, profit factor at least 1.1, and a positive net return. That is the same floor the desk uses (min PF 1.1, min 12 trades).

The code list is [src/lib/proven-wide-trail.ts](../src/lib/proven-wide-trail.ts). On the engine it is pinned in addition to the full 2,328-set catalog, so each of these is built through the whole protect grid (wide, short, minimal) and ranked even if one Base window misses the gate. The saved preset is **Wide trail proven**. Applying that preset alone would narrow the book to these 24; the live desk does not do that. The pin is the additional path.

Minimal and short ranges are not in this list. They did not clear the gate.

| Pair | PF | Closes | Net return |
|---|---:|---:|---:|
| sweep \| dir-vwap-240 @ 15m combined | 3.11 | 23 | +2.02 |
| follow \| rsi-mom-21-20 @ 1m | 2.36 | 14 | +0.17 |
| sweep \| ema-50-100 @ 15m combined | 2.34 | 27 | +1.34 |
| follow \| rsi-mom-21-25 @ 1m | 2.30 | 211 | +3.87 |
| sweep \| ema-50-100 @ 30m | 2.28 | 13 | +0.95 |
| follow \| r-vol-regime-m @ 15m | 2.11 | 18 | +0.71 |
| sweep \| trend-ema-50-200 @ 15m combined | 2.10 | 25 | +1.89 |
| sweep \| hma-55 @ 30m | 1.76 | 26 | +0.78 |
| follow \| move-swing-32 @ 30m | 1.68 | 18 | +0.64 |
| follow \| rsi-mom-14-20 @ 1m | 1.64 | 268 | +2.92 |
| sweep \| dir-emax-12-26 @ 30m | 1.49 | 54 | +1.38 |
| sweep \| ema-slope @ 30m | 1.41 | 38 | +0.80 |
| revert \| dir-emax-5-13 @ 15m | 1.37 | 94 | +1.98 |
| follow \| r-connors-m @ 15m | 1.33 | 37 | +0.75 |
| sweep \| macd-zero @ 30m | 1.26 | 48 | +0.73 |
| follow \| ichi-cloud-20 @ 5m combined | 1.26 | 14 | +0.08 |
| sweep \| ichi-cloud-9 @ 30m | 1.18 | 34 | +0.28 |
| sweep \| dir-vwap-240 @ 30m | 1.18 | 19 | +0.35 |
| sweep \| ema-9-21 @ 30m | 1.15 | 111 | +1.05 |
| sweep \| dir-emax @ 30m | 1.15 | 111 | +1.05 |
| sweep \| dir-emax-5-13 @ 30m | 1.14 | 60 | +0.40 |
| sweep \| trend-ema-20-50 @ 30m | 1.14 | 40 | +0.37 |
| sweep \| ichi-tk-9 @ 30m | 1.14 | 55 | +0.45 |
| follow \| ema-stoch @ 15m | 1.14 | 45 | +0.27 |

Six of these are in the open book right now: `revert|dir-emax-5-13@m15`, `sweep|dir-emax-5-13@m30`, `sweep|hma-55@m30`, `sweep|ichi-tk-9@m30`, `follow|ema-stoch@m15`, `follow|r-connors-m@m15`.

Ten open pairs are not in the set. They are still held until they close, and they are not labeled successful: `follow|bb-walk-50@m30`, `follow|trend-ema-12-26@m15c`, `revert|bb-mid@m30`, `revert|r-fakeout@m30`, `sweep|dir-vwap-30@m30`, `sweep|kelt-20-2@m15`, `sweep|trend-adx-20@m15c`, `sweep|trend-adx-20@m30`, `sweep|trend-adx@m30`, `sweep|trend-ema-5-20@m30`.

Signal sources posted higher profit factors and are not in this set. Signals processing is off, and those ids are not engine configs.
