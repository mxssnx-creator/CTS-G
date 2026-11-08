# Fee and replay audit — 2026-11-08

The configured fallback is maker 0.0005 and taker 0.0008 (fractions: 0.05% and 0.08%). Slippage is a fee ratio of 0.1, i.e. 10% of the applicable fee. Taker/taker paper allowance is 0.176%, maker/taker 0.143%, maker/maker 0.110%. These are operator assumptions, not measured venue rates.

Account-specific rates supersede fallback estimates only within their symbol/account scope and freshness window. Unknown selected symbols continue to contribute their fallback. Actual fill prices already contain slippage; measured fees, confirmed zero and explicit normalized rebates are not charged an additional paper allowance. Do not treat a LIMIT order as maker evidence.

This source update does not start or deploy a trading service. Candle simulations cannot reproduce queue priority, unknown intrabar paths, latency or liquidity. No profitable configuration was promoted from this sample, and high settled-only PF does not establish high marked or future PF.

## Evidence window

8 cached symbols (ADA, AVAX, BCH, BNB, BTC, DOGE, DOT, ETH), 2026-11-06 19:00 to 2026-11-07 19:00 UTC. First 12 hours are prehistory; last 12 hours are scored. Data was already cached; no private account rates or credentials were read.

| Project / scope | Closes | Settled PF | PF including open marks |
|---|---:|---:|---:|
| CTS-G shared parent replay, new cost | 17 | 10.602 | 0.931 |
| CTS-G same replay, legacy fixed 0.12% | 17 | 9.312 | 1.135 |
| CTS-A-O native runtime, 12h selection | 0 | unavailable | unavailable |
| CTS-A synthetic Axis + Block | 9 | 0.80985 | not comparable |

CTS-G fast replay (cached, hourly diagnostic sampling) took 7 s versus 38 s for the detailed cold run. Exact closed trades and open positions matched; 60 sampled production gate/rank checks passed. Validation always remains per entry. Diagnostic sampling density is not the admission cadence.
An initial CTS-A-O run took 92.5 s with four workers versus 246.3 s with one worker, but selection settings differed, so this is not a controlled speed benchmark. Its coverage checks failed: 2,099/3,288 requested Base combos returned; 36 pairs passed Base and 0/14,042 later config evaluations passed. 608 Micro candidates failed the close-count requirement. Do not replace these zero executions with historical selected-window PF.


## Further native checks and Micro research

Adding cached indicator warmup (49h of physical candles, still only 12h evaluated prehistory + 12h scored) allowed CTS-A-O to execute 34,711 modeled closes, settled return PF 8.37235. This is **not** a successful live preset: 15,532 order allocations remain open across seven positions, the modeled $10 book ends at negative $9.79 equity, and exposure is infeasible for 541 minutes. The high settled PF conceals open losses. The run took 170.9 seconds. Most closes were Minimal; Micro indications participated but the separately named Micro range did not qualify. Requested slow lanes still lack enough prehistory.

A separate CTS-A-O Micro study tested 48 first-class source/timeframe/profile combinations plus the runtime's automatic legacy Micro focus: 1/5/15m, 15-minute hold, net targets at 1/1.5/2 times the 0.176% fallback, stop ratios 1/1.5/2 and four trailing choices. It evaluated 816 Micro pairs, passed none and executed none (3 seconds). Median evaluated Micro PF was 0.1051. This losing research grid is not a new default.

CTS-G's actual Pulse parent position manager was driven offline on an explicit OHLC tick path for the 17 replay entries. All 17 exit reasons and exit bars matched. Stub market prices and fee-only estimated settlement differ from barrier-fill paper prices; these differences are retained. This is parent lifecycle evidence, not full account execution proof. Two Axis replay defects are fixed: open positions consume continuation slots through the end, and last-N/loss streaks follow settlement order. Both new regressions pass.

## Implementation and verification

`fee_policy.py` owns the fallback values and ratio. Pulse and `stage_eval.replay_overlay` resolve the same cost; an optional `STAGE_FEE_SNAPSHOT` contains `asOf`, `selectedSymbols`, and per-symbol `makerRate`, `takerRate`, `at` (seconds). Snapshot bytes and the resolved cost enter cache provenance. Axis studies now reject unverifiable/stale caches. The first aligned trading hour is no longer skipped.

Actual account refresh stays on the existing read-only commission endpoint; actual closes use fee-only fallbacks. Zero modeled cost is preserved. UI controls specify fee rates and a fee-relative slippage allowance. Historical regression fixtures explicitly pin their prior 0.12% model; fee-policy checks separately exercise the new defaults and mixed known/unknown schedules.

Verification: full 631-check run returned 629 passes and two DCA fixture failures. Those fixtures mixed the old modeled cost with the new fallback; after explicitly aligning their modeled cost and independent fee oracle, both targeted DCA checks passed. All other checks in that run passed. TypeScript checking and build passed. The full live Block order lifecycle is exercised with stub-venue tests, not by the market parent replay. No claim of full account-level replay parity is made.

Reproduce fast market report:
```
python3 scripts/trade_sim.py --data MARKET_DIR --cache CACHE_FILE --symbols 8 --jobs 4 --last-hours 12 --axis-current --eval-every 3600 --stage-details --patch '{"histLookbackBars":720,"histLookbackAuto":false}' --out REPORT_PREFIX
```
`tpRanges` restricts the general packs. The independent Short Range pack and `trailRanges` have separate controls. A Micro-only experiment must constrain those too; do not label an experiment Micro-only if it still contains Minimal or general trailing orders.

## Step PF and window-statistics update
The existing per-step floor remains shared by Base/Main/Real. Statistics now label actual per-range window lengths, including independent Real-negative lengths, and retain configured-set histograms. Each set snapshot exposes its effective floor and complete stage-window policy. The stage oracle passed 28 checks, including 18,560 orders across 29 window configurations. A fresh 8-symbol 12-hour scored replay retained exactly the same 17 closes and 8 held positions as the pre-statistics change: settled PF 10.602, closed-plus-marked PF 0.931. No future PF guarantee follows.
