# Slow indications for Normal / Long / Wide, and an adaptive direction rule (2026-11-15)

Owner: "Add, workout indications which are working with Normal, Long, Wide coords" / "try all kind of tactics and logics, coords until its working".
No setting was changed by this study. Scripts: scratchpad `slow_screen.py`, `flip_screen.py` (bracket outcomes per signal onset, both directions).

## 1. Screen (development window only, 4 symbols, 11-12 16:00 .. 11-14 00:00)
624 names (`_n14/_n21` on 15 / 30 min, `_c14/_c21`, `_x14/_x21`), onsets every 5th bar, fixed brackets TP 1.5 / 2.5 / 4.0 % (Normal / Long / Wide),
SL 0.5 / 0.75 x TP, hold 360 min, net of 0.12 %. Pool of all onsets: PF 0.79 / 0.74 / 0.49 (SL 0.5). 236 of 2,430 cells above 1 in both halves; the
leaders are 30 min reversion families (bbrev, sweep, vwapz, willr, stoch, mfi, ao_axisrev; PF 3 - 20 at TP 2.5 %).

## 2. The 12 leaders elsewhere (PF of every onset, TP 2.5 % / SL 0.5)
- 16 other symbols, the same development hours: 2.01 (2,144 onsets): same hours, correlated market, not independent.
- Sealed 16 h (11-14 00:00 .. 16:00), 10 symbols: **0.097** (811). The reversion signals lose almost every trade once the market trends.

## 3. Adaptive direction (follow while the family's last K closed onsets read PF >= 1, else the opposite direction; closes before the entry only)
One position per symbol, stops filled at 1.15 x (live x02), TP 2.5 % / SL 0.75, K = 40, family scope (PF / onsets / net %):

| window | follow | inverse | adaptive |
|---|---|---|---|
| development 11-12 .. 11-14 (reverting) | 1.05 / 123 / +6.0 | 0.72 / 122 / -34.4 | 1.02 / 114 / +1.9 |
| sealed 11-14 00:00 .. 16:00 (trending) | 0.08 / 56 / -82.7 | 5.53 / 51 / +52.7 | 4.54 / 51 / +49.4 |
| new 13 h 11-14 16:00 .. 11-15 05:00, fetched after the screen | 0.48 / 117 / -87.0 | 1.33 / 107 / +31.4 | 1.45 / 104 / +39.5 |
| **older 11-10 01:00 .. 20:00 (8 symbols, independent)** | 0.85 / 47 / -7.3 | 0.80 / 46 / -9.6 | **0.54 / 41 / -24.8** |
| **older 11-10 20:00 .. 11-11 16:00** | 0.72 / 36 / -9.7 | 0.52 / 30 / -13.3 | **0.49 / 34 / -18.5** |

The rule held across one reverting and one trending stretch and failed on the older, choppy one, where neither direction reaches its target.
**Not built.** It is the persistence sign flip recorded since 2026-11-02 again: a rule chosen on one or two regimes does not carry to the next.

## 4. Through the engine (trade_sim, 4 symbols, x02 overlay + the 12 names and their `@I` inverses as indicationConfigs packs, steps 13 - 32)
Executed PF / orders / net: development 1.051 / 144 / +6.05 % (shipped 1.054 / 127 / +5.82 %), sealed **0.524 / 70 / -29.29 %** (shipped 1.082 / 51 / +3.72 %).
The slow packs executed 6 and 3 orders (a Set needs its own recent orders to pass Base / Main / Real), and their tapes moved the pooled Auto floors / SL
ranges of every other pack (the other packs' sealed PF fell to 0.57). Not shipped.
