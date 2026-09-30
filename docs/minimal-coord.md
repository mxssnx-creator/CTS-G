# Minimal Coord.

Independent part for the two close protect ranges. The wide grid is not in this part. The code is [src/lib/minimal-coord.ts](../src/lib/minimal-coord.ts). The live desk on x02 does not import that file; it still runs the same cell rule inside the engine. This copy is the saved, standalone form.

Read at 2026-09-30 15:55 CEST from `bingx-vst-02` (VST), engine compute #69.

## What the part is

A cell is one take-profit, one stop multiple, one trail and one hold. Round-trip cost is 0.2% (0.1% per side). A trailing cell forces the stop to at least 2× the target, so the stop sits further out than the trail. Two holds, 16h and 24h, are shared with the wide grid.

| Range | Take-profit | Stop × target | Trail × target | Floors |
|---|---|---|---|---|
| Minimal | 0.40, 0.45, 0.50, 0.55, 0.60% | 1, 1.25, 1.5, 1.75, 2 | 0, 0.75 | stop 0.20%, trail 0.10% |
| Short | 0.60, 0.80, 1.00, 1.20% | 1 to 3, step 0.25 | 0, 0.50, 0.75 | stop 0.60%, trail 0.20% |

Before identical cells collapse, that is 100 minimal cells and 216 short cells (2 holds). Trailing cells share a stop, so the list that is actually computed is shorter. Nothing in this part builds the wide targets (3, 5, 8, 11%).

The source constant for the short stop list is the 0.25 step above. The running saved grid is narrower: stops 1, 1.5, 2, 2.5, 3 only. Minimal in the saved grid matches the table.

## What is configured

x02, live on, 50 symbols, 2,328 config sets (every bot × indication). Focus is that full set. Base on the last compute: 16,272 lane combos, 1,104 passed. Book profit factor 1.82 on 221 simulated trades. Validate last 50, live last 25.

| Toggle | State | What it does to orders |
|---|---|---|
| Trailing | on | Wide cells with a trail are eligible |
| Block | on | Size multiplier, not its own order |
| Block Active | on | Block starts at level 3 |
| Axis | on | Eligible. None of the open positions are Axis |
| Normal | off | The family toggle is off. A wide cell with trail 0 is still labeled flat |
| DCA | off | Not armed |
| DCA Active | off | Not armed |
| Signals | off | Not armed |

Live control is overall: many internal positions of one symbol and side become one exchange position. Notional is about $3. Cap is 200 exchange positions. Orders per symbol are unlimited.

## What actually executed

Closed paper trades in the desk database: 8,404. The take-profit on the config id is scaled by the timeframe lane (15m is the reference, 30m is ×√2, faster lanes are scaled down and floored). Counted after undoing that scale:

| Range | Kind | Closed | Win rate | Closed PnL |
|---|---|---:|---:|---:|
| Wide | trailing | 3,954 | 65.7% | +1,118.44 |
| Wide | flat (no trail) | 4,325 | 59.2% | −325.02 |
| Wide | DCA | 100 | 59.0% | −109.57 |
| Short | Axis | 6 | 66.7% | +5.98 |
| Short | DCA | 13 | 69.2% | −21.72 |
| Short | trailing | 6 | 16.7% | −19.90 |
| Minimal | any | 0 | — | — |

Short closes that did happen are the 0.80% and 1.20% cells (and the same cells on 30m, which print as 1.13% and 1.70%). They were old indication pairs (`rsi-mom`, `r-vol-regime`) and signal sources (`sig-s2-…`), not the positions open now. Net of those 25 shorts is negative. Minimal has no closed trade in this database.

The only book that made money is wide trailing.

## What is still executing

54 internal positions. All long. All wide. No minimal cell. No short cell. No Axis. No DCA.

43 are trailing. 11 are the same wide grid with the trail off (the id contains `tr0`). Block only changes size; it is not a separate position.

| Strategy set | Open | Targets still working |
|---|---:|---|
| revert \| dir-emax-5-13 @ 15m | 17 | 11% target, stop 11% or 16.5%, trail 5.5% |
| revert \| bb-mid @ 30m | 7 | 11.31% (8% × √2), flat and trail 8.49% |
| revert \| r-fakeout @ 30m | 4 | 11.31%, trail 5.66% |
| sweep \| dir-emax-5-13 @ 30m | 4 | 15.56% (11% × √2), trail 7.78% |
| sweep \| trend-adx-20 @ 15m combined | 4 | 11%, no trail |
| sweep \| dir-vwap-30 @ 30m | 3 | 15.56%, trail 7.78% |
| sweep \| ichi-tk-9 @ 30m | 3 | 15.56%, trail 7.78% |
| sweep \| trend-adx-20 @ 30m | 3 | 11.31%, one flat and two trailing |
| follow \| ema-stoch @ 15m | 2 | 5% trail 3.75%, and 8% flat |
| follow \| trend-ema-12-26 @ 15m combined | 2 | 3% target, stop 6%, no trail |
| sweep \| kelt-20-2 @ 15m | 2 | 11%, trail 8.25% |
| sweep \| hma-55 @ 30m | 1 | 15.56%, trail 11.67% |
| sweep \| trend-adx @ 30m | 1 | 11.31%, trail 8.49% |
| sweep \| trend-ema-5-20 @ 30m | 1 | 15.56%, no trail |

Symbols in that book: AVAX, ETHFI, GALA, ICP, JUP, NEIROCTO, PLUME, PUMP, SYRUP, TIA, VIRTUAL, ZORA.

## x02 VST positions

Account `bingx-vst-02` at the same read:

| | |
|---|---|
| Equity | $492,395.29 |
| Open PnL | +$139.94 |
| Margin in use | $139.95 |
| Exchange positions | **58** |
| Working orders | 77 (75 long, 2 short) |

Those 58 are the account, not 58 copies of the 54 above. Overall control keeps one exchange position per symbol and side.

11 of the 58 sit on symbols this desk's order log has actually sent: AAVE, AVAX (long and short), ETHFI, NEIROCTO, NIGHT, PLUME, SYRUP, VIRTUAL, XPL, ZORA. The other exchange positions are on this VST account but are not in the desk's order log, so they are not counted as opened from here.

GALA, ICP and PUMP are in the 54 and are not on the exchange right now. JUP and TIA are in the 54 and on the exchange, but not in the recent order log.

So: **54** positions are the ones this desk is executing. **11** exchange positions match symbols it has sent orders for. **58** is the whole VST account.
