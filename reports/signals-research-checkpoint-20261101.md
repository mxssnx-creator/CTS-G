# CTS-G Signals, costs and order protection — 1 November 2026

> Historical checkpoint. Current validation: [Signals coordination and x02 acceptance](signals-verified-20261101.md).

Research checkpoint, not a production approval. No exchange orders were sent,
no x01/x02 service was started or stopped, and no remote installation was made.
All Signal executions below are modeled. No positive-hour or future-profit
guarantee is supported by these results.

## Changes in the existing application

- Signals use a minimum PF of **2.2** in engine defaults/loading, shipped
  overlays, presets, settings loading and saving. A higher explicit Signal
  threshold survives; legacy lower values are raised. Ordinary strategy PF
  thresholds are unchanged. This is a gate, not a promise about subsequent PF.
- Overall controls use outer position ranges, including the explicit outer
  stop overlay. Control coverage uses the full remaining quantity (after
  partial fills), not a 95% allowance or the presence of order IDs.
- Close-all protection and quantity-limited protection remain distinct.
  Stacking/refresh and trailing fallback create confirmed replacement
  protection before cancelling old protection. Failed replacements retain
  prior controls; uncertain atomic replace outcomes remain unverified.
- Reconciliation respects connection ownership and position direction,
  retains tighter protective stops, records returned prices/coverage, and
  retries duplicate cleanup. Already-existing responses must resolve to a
  full-coverage order with a finite reported price.
- Coverage UI shares one interpretation of missing, partial, unverified and
  reported coverage. Old snapshots do not become green just because IDs exist.
- Hourly statistics distinguish cash PF from return PF, retain recorded net
  costs, use closed quantity for partials, and distinguish empty/flat/partial
  or unavailable hours. Closed cash is not mislabeled as account equity profit.

The pinned trader blob is `d650dac73080e853d21ce4dd5183fafb0ca660cd`.
The deployment pin and recovery patch reproduce that blob. These are code and
fixture checks, not confirmation that a venue accepts every payload variant.

## Separate offline Signal model

`server/pulse/signal_replay.py` has no exchange client. Every exact
source/range/timeframe/symbol/direction/protection/Last-N branch owns its own
Base, Main and Real histories and shadow positions. Main/Real re-simulate
admitted entries rather than filtering a future Base tape. Decision evidence
requires a close strictly earlier than the decision timestamp.

Only complete UTC-aligned candles are used. Missing minutes, unknown settings,
unsupported types and oversized batches fail explicitly. Maximum batch size
remains 12,000 exact contexts; this study splits profiles instead of silently
dropping configurations. Normal admission can be off while internal histories
continue; the trailing master removes trailing contexts. Maker/taker fees use
each leg's own notional and slippage is adverse on both legs.

The modeled capital book uses explicit exposure/open-order limits. A loss
guard pauses new admissions while exits continue. An identified reporting bug
understated drawdown when a gap occurred at admission: opening-price and entry
fee marks are now retained and independently rebuilt. Intraminute drawdown,
funding, liquidation, margin, partial venue fills and venue netting are not
modeled. No hard loss bound is claimed.

## Source categories and primary research

The original six families were expanded with three research-only families.
Definitions were fixed before testing; there was no post-result winner tuning.
All use separate short/medium identities and 1/5/15/30-minute completed bars.

| Source | Category | Short | Medium |
|---|---|---|---|
| EMA cross | Trend | EMA 9/21 | EMA 21/55 |
| EMA trend | Trend confirmation | EMA 12/26 with close confirmation | EMA 20/50 with close confirmation |
| MACD cross | Momentum transition | 8/21/5, six-bar hold | 12/26/9, six-bar hold |
| RSI reversal | Mean reversion | RSI 7, 15/85 | RSI 14, 25/75 |
| RSI momentum | Momentum | RSI 14, 20/80 | RSI 21, 20/80 |
| Donchian | Price breakout | Prior 10-bar channel, six-bar hold | Prior 20-bar channel, six-bar hold |
| Bollinger bounce (new) | Volatility / mean reversion | SMA 20, two population standard deviations | SMA 50, same band rule |
| CCI reversal (new) | Typical-price deviation | CCI 14, ±100 | CCI 20, ±100 |
| OBV trend (new) | Volume confirmation | Signed-volume balance vs EMA 20 | Signed-volume balance vs EMA 50 |

Primary documentation consulted on 1 November 2026:

- [TradingView: Bollinger Bands](https://www.tradingview.com/support/solutions/43000501840-bollinger-bands-bb/) defines the moving average and deviation bands.
- [TradingView: CCI](https://www.tradingview.com/support/solutions/43000502001-commodity-channel-index-cci/) defines typical price, mean absolute deviation and the 0.015 scaling constant.
- [TradingView: OBV](https://www.tradingview.com/support/solutions/43000502593-on-balance-volume-obv/) defines signed volume accumulation, including unchanged closes.
- [TradingView: Stochastic](https://www.tradingview.com/support/solutions/43000502332-stochastic-stoch/) was reviewed as a range-momentum candidate; no separate Stochastic family was added in this checkpoint.

These sources support formulas and categories, **not profitability**. Threshold
crossings can persist in trends rather than predict reversals.

The pinned CTS-A-O reference is `2599b652b9429c34d290a07e8df1d70a62d80462`.
Its checkout was read only. The original six families matched 57,600 fixture
states. Expanded comparison retains **821 differences / 86,400 states**:
818 Bollinger differences (near-flat variance cancellation and exact-band
rounding) and three near-flat CCI rolling-mean differences. These were not
silently hidden or called parity success. Centered calculations avoid
catastrophic cancellation; separate 60-digit Decimal checks validate the
corrected Bollinger/CCI states. See `signals-source-parity-final-20261101.json`
and `scripts/test_signal_source_math.py`.

## Trailing-only and historical selection

The wide profile tests gaps of **0.5%, 1%, 2%, 3% and 5%**, each with activation
0.5 percentage points above its gap, a 1% initial stop and a 120-minute modeled
hold limit. These are diagnostic scenarios, not recommended live settings.
Every wide profile has `kind=trailing-only` and **TP=0**. Nonzero TP is rejected
for trailing-only. Long/short tests cross a hypothetical fixed target without
closing, then verify the actual trailing stop. Gaps/slippage can still lose
more than a configured stop, and time exits can still lose money.

Frozen-selection cases build the exact eligible context list using only
pre-trading Base/Main/Real closes. They cannot admit a context that only looks
good later. All ongoing gates remain active. Empty selection is retained,
not bypassed with a lower PF, borrowed sibling history or invented samples.

## Frozen experiments and outcomes

Feed SHA-256:
`bd4b66e09c65b990082ccc56dfd2624fd18815d01767a315dc002dfa0f6f02b0`.
Exactly eight symbols, 12 hours prehistory followed by 12 hours modeled
trading, 8 October 2026 12:00–9 October 2026 12:00 UTC (14:00–14:00 Vienna).
The archive was studied previously: this is **not an untouched holdout**.
Each result stores configuration, data/source hashes, all context outcomes,
execution records, open positions, hourly marks and measured resource usage.

| Comparison | Settled / open | Closed-return PF | Ending equity change, from 1,000 |
|---|---:|---:|---:|
| Original three sources, former PF 1.3 | 55 / 2 | 1.025100 | -0.138254 |
| Original three sources, PF 2.2 | 31 / 1 | 0.732791 | -0.790545 |
| Six-source expanded low/middle grid, PF 2.2 | 61 / 1 | 0.706179 | -1.617727 |
| Six sources, common three-protection grid | 45 / 1 | 0.812145 | -0.921648 |
| Nine sources, same common grid | 49 / 1 | 0.795153 | -1.177300 |
| Nine sources, higher-cost stress | 14 / 0 | 0.222037 | -2.055056 |
| Nine sources, wide trailing-only | 12 / 3 | 0.739579 | -0.605238 |

None of the seven new category scenarios ends net-positive. The three frozen
selection arms open no modeled orders: no exact context passed the full
pre-trading chain. For the common nine-source grid, prehistory has 169 Base
passes, three Main passes and zero Real passes. That is insufficient evidence,
not an instruction to force orders. At the end, passing-window PFs are
4.329893 / 3.670120 / 3.308734, while the corresponding all-close PFs are
0.662431 / 0.783974 / 0.901255. Selecting only passing windows would conceal
the losing population and modeled portfolio.

Common nine-source replay: 6,912 contexts, 4.9 seconds and about 88 MiB peak RSS.
Wide replay: 11,520 contexts, 7.7 seconds and about 123 MiB peak RSS. These are
individual measurements under concurrent load, not full production-engine
throughput guarantees. Two study workers leave CPU/memory headroom.

Readable evidence:

- `signals-pf22-results-20261101.md`: all five PF-2.2 diagnostic scenarios and every trading hour.
- `signals-categories-results-20261101.md`: all seven new scenarios and every trading hour.
- `signals-categories-stage-grid-20261101.md`: 1,296 common-grid configuration/stage lines.
- `signals-pf22-stage-grid-20261101.md`: 1,728 expanded configuration/stage lines.
- `signals-categories-final-20261101/*.json.gz`: every exact context and frozen selection identity.

## Earlier positive evidence is preserved, not relabeled

The older ordinary cohort independently recounts to 336 closes / PF 3.696049
with unchanged identities. It is not the new Signal model. The read-only
archived CTS-A-O `docs/signal-pf-drop-20261025/README.md` records a paired
Signal experiment: 160 settled / PF 8.381491 in one window and 121 / PF 0.052474
in the next, using 150 hours prehistory. Another archive,
`docs/signal-pf15-20261020.md`, records selected development PF 3.839497 and
confirmation PF 1.370291. These are retrieved archived results, not newly
executed replays. Different periods, context lengths and strategy models
cannot be claimed equivalent. Both successful and unsuccessful windows remain
part of the evidence.

## Verification and remaining release blockers

Completed focused checks: 21 replay contracts, four independent source-math
tests, 14 control-coverage fixtures, 27 logistics checks, 64 available TS unit
tests and 109/109 engine functional checks. The PF-2.2 differential reconstructs
27,648 stage-context comparisons with zero differences, plus all five
scenarios' cash, counts, close marks and admission marks.

The expanded category differential additionally reconstructs **69,120**
stage-context comparisons (new sources, common nine-source grid and wide
trailing), with **zero differences**, including pre-trading selection.
All seven scenarios' modeled cash/counts/close and admission marks agree.
The full regression is recorded separately when its running checks finish.
Four edited TSX files parse successfully.
The broader Node baseline passed 191 tests with four optional skips. Local
production build/typecheck/render and two TS dependency-loading suites remain
unverified because TypeScript/Vite/TanStack Start/jose are unavailable here.

Still incomplete: full CTS-A-O Signal runtime adapter, MFE fitting, DCA/-Active
Signal semantics, simultaneous ordinary-runtime Last-N lanes, host-aware
service resource sizing, genuine x02 fills/partials/stacking/netting and
protective-order acceptance, fresh forward/OOS evidence and release UI QA.
Nothing here establishes profitable live operation or complete systemwide
correctness. No source or preset is promoted to production by this checkpoint.
