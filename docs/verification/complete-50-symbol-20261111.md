# Complete 50-symbol replay and execution audit

The native Main-short scorer sorted equal-clock observations by entry time and
settled return. The stage chain kept the original tape order. The immutable
50-symbol baseline exposed two mismatches in 60 sampled comparisons. Sorting
only by close time preserves stable tape order; the corrected baseline passes
60/60. No later loss is removed and no selected PF is copied into executions.

The recovered Tactic/Inverse integration now retries an asynchronous result in
the same completed minute, supports the live book's retained stage context,
receives the actual symbol and reference-market data in replay, and retains
source availability. Source assets participate in cache provenance. All 2,918
imported source identities have a separate golden-state check.

Normal and Trailing controls remain independent by range. Enabled additional
strategies retain their calculated parent evidence. Live entry volume uses the
configured indication/type/range/symbol weights; reports distinguish unit
returns from nominal volume-weighted results and exchange-sized actual fills.

Replay, stack and range studies resolve the same fee schedule. Fallback taker
0.08%, maker 0.05%, and slippage at 10% of the fees imply a taker/taker paper
budget of 0.176%. Actual fill prices already include slippage and actual fees
are deducted once on each fill's own notional. A mixed maker/taker fallback is
0.143%. These are configured assumptions unless account-rate evidence exists.

DCA replay adds now use their own entry notional when contributing to the
original parent's cash result. Winning exit candles do not assume a favorable
intrabar ordering. Block rounds and Block-add studies decide before closes at
the same clock. A hand case previously admitted two Block adds using eight
same-clock closes; it now admits zero.

Trend/Break aggregation uses an exact-content, per-thread memo capped at 16
windows. ATR computes only the contributing tail and its preceding close.
Independent arithmetic checks preserve the former results. The same 600-bar
profile measured 36.4 seconds before and 18.6 seconds after; this is a profiling
sample, not a guaranteed systemwide speedup.

The frozen input consists of 50 distinct 1-minute histories, 4,320 candles each,
2026-11-08 17:00 through 2026-11-11 17:00 UTC (end exclusive). The first 48 hours
are prehistory and the final 24 hours are scored. Timestamps, duplicates,
finite values and OHLCV geometry were checked. All hours and losses are kept.

The breadth diagnostic declares 97 packs and 4,272 Sets: all 60 native
indication families at 1m, selected additional 5m/15m/30m cases, 11 imported
Tactic/Inverse sources, and the eight native packs. It uses a thinner declared
distance grid and representative constituent lists for combined packs. It is
not an exhaustive sweep of every parameter or every imported catalog source.

Per-symbol checkpoints bind the full market universe, settings, Python sources,
TypeScript assets and simulator bytes. Completed symbols resume only under the
same identity. The ordinary replay cache still rejects changed dependencies.
The isolated baseline clock-order migration is separately guarded by a full
AST comparison permitting exactly the one scoring-only edit, verifies the old
source tree and cache bytes, and requires native parity before writing a new
cache. Original evidence is retained.

The self-contained HTML generator reports hourly settled and entry-cohort
results separately, stage full-N sufficiency, each configured indication and
strategy type, ranges, direction, all symbols, held marks and the closed-order
ledger. Structural/script checks do not constitute a browser-render check.

The original-grid native replay closed 364 positions at PF 0.296, while its
selected Base windows reported PF 2.908. The populations differ; selected PF
does not prove later profitability. The breadth arm changes its configured universe and grid, so the comparison
does not isolate a single bug or strategy. Both runs use 0.176% paper cost.

An optional immutable-tape prefix index caches native causal stage membership
per configuration and parameter snapshot. It recomputes the current short
vote from earlier closes only, and uses the native implementation when context
expires or a dynamic tape adapter is present. All baseline output fields,
including every one of the 364 executed positions, exactly matched the full
native replay. Cached replay runtime fell from 2,593 to 616 seconds (4.209x on
this case). Synthetic oracles cover ties, cutoffs, changed costs and parameters,
negative vetoes, dynamic sources and identical portfolio orders.

Large replay signals now use lossless signed-integer and binary64 columns.
Next-entry cohorts merge only the requested entries from each ordered symbol
lane, instead of materializing every configuration's complete future history.
Large comparison arms run sequentially to avoid concurrent memory peaks.
The stored shadow bytes and their provenance are unchanged by packing in RAM.

Every exit-hour comparison uses h < close <= h+1h, including all intermediate
hour boundaries. Equal-clock drawdown events retain source order rather than
sorting by their result. The HTML distinguishes hour-end drawdown from an
intrahour maximum. It includes every configuration, 11 charts, all strategy
hourly arms and the independent actual-fill cash reconciliation. No-loss
strategy windows are labeled explicitly rather than promoted as finite PF 99.

VST connectivity subsequently recovered. A bounded x02 acceptance run passed
30 checks: two directions, eight market fills, two position increases, four
overall protections, partial remainders and final closes. All test positions
ended flat, with no own pending orders. The first attempt's minimum-lot
rejection was recovered and cleaned before retry. Explicit rejection is now
distinguished from an uncertain write, client-ID case is preserved, and cleanup
requires the exact journaled remainder rather than a maximum-size bound.

An independent Decimal cash ledger from venue fill history agrees with
execution_result within 1e-10 VST for both completed cases. Reported commission
is deducted once on each fill. The observed account rates were taker 0.05% and
maker 0.02%, separately labeled from paper fallback assumptions. This run
verifies bounded exchange acceptance, not engine qualification or profitability.
