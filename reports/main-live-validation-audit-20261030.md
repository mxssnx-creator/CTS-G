# Main, indication, Axis and Block audit — 2026-10-30

Operational findings below are superseded by [the execution-cost and lane audit](execution-cost-and-lane-audit-20261030.md). The bounded market sample remains a historical replay result.

## Findings and corrections

- Main short-window choice uses its own complete, causal paper Main evidence. Executed fills are now separate; they cannot add votes or duplicate paper orders. Compact entry-time paper evidence survives the display cap and expires with the lookback.
- Scoring excludes expired and future results, keeps a monotonic event clock when closes arrive out of order, and applies the current round-trip cost consistently to statistics and candidate votes. Profit factors are calculated before display rounding, including when results are very small.
- Replay now dates each symbol from its newest completed exchange bar. The kline adapter removes duplicate candles, keeps the newest continuous run, rejects malformed rows, and prevents future candles from being used. Hold candidates and chunked tapes obey the same as-of boundary.
- Active and Common indication switches and Direction, Move, and Active range settings now reach the Set's actual replay/live signal. The Active vote evaluates its configured ranges and uses the strongest qualifying result. The other enabled indication types retain their own votes.
- Axis pause streaks use the same net return after PositionCost as the Axis PF. Paper evidence is time-bounded and held in bounded heaps. Pack/lane gates are independent: a pooled losing lane does not block an unrelated pack. A valid weaker pack and a valid alternate lane remain eligible when a stronger one is unavailable. A disappearing Set cannot cause placement of an untagged position.
- Equal-time closes from different symbols are tested as a multiset, not by a timestamp-keyed dictionary. The pinned trader and its installer restore patch are regenerated from the same source.

## Short market sample

The source is the saved 2026-10-14 BTC, ETH, SOL, and DOGE one-minute sample. Three six-hour snapshots span 11 hours of input, use a 0.12% round-trip cost, one SL ratio, four trailing variants, and all eight available strategy packs. They are replayed paper orders from a reduced grid, not exchange fills. The selected Base and Main PFs are in-sample; overlapping configuration Sets may represent the same market opportunity. A PF of 99 is the engine's sentinel for a positive sample with no losses, not an estimate of future PF.

| Snapshot UTC | Base validated / configured | Base selected PF | Base selected orders | Main validated Sets | Main selected PF | Main window orders | Final eligible Sets |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 18:08 | 178 / 438 (40.6%) | 20.0207 | 3,336 | 174 | 28.7416 | 2,592 | 174 |
| 20:38 | 159 / 438 (36.3%) | 37.4792 | 2,295 | 120 | 54.1170 | 1,688 | 121 |
| 23:08 | 325 / 438 (74.2%) | 9.3053 | 4,208 | 287 | 34.4693 | 2,523 | 287 |

These rows use positive minimal coordination of one result. The final number includes either qualifying lane, so it can differ from the positive Main count. In the negative-minimal run, the same snapshots had 32, 8, and 4 selected Main-window observations respectively, no loss in those tiny selected samples, and **zero final eligible Sets** in all three. This does not establish a viable negative lane.

Representative positive-mode groups at 23:08, grouped by strategy, Set type, and step range. The Base PF and Base orders are pooled over that group's Base-validated Sets; Main and Real paper counts include the resulting paper streams and are not executed fills.

| Strategy | Type | Steps | Base / Main validated Sets | Base PF | Base orders | Main / Real paper orders |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| indications | base | 7–12 | 6 / 6 | 6.2003 | 105 | 80 / 56 |
| indications | exit | 13–20 | 8 / 8 | no losses | 136 | 104 / 72 |
| indications | trail | 2–6 | 1 / 1 | no losses | 17 | 13 / 9 |
| general | base | 21–30 | 10 / 10 | no losses | 100 | 70 / 60 |
| trend | trail | 7–12 | 2 / 2 | no losses | 22 | 16 / 10 |
| breakout | base | 7–12 | 6 / 6 | no losses | 36 | 24 / 12 |
| reversion | exit | 13–20 | 8 / 0 | 1.9193 | 152 | 32 / 8 |
| flow | base | 21–30 | 10 / 10 | no losses | 110 | 70 / 40 |
| multi | base | 7–12 | 6 / 6 | 5.8832 | 105 | 80 / 56 |
| shortrange | all | 2–6 | 0 / 0 | no sample | 0 | 0 / 0 |

Across the same last six-hour sample, the four symbols produced distinct signal streams: eligible signals per symbol ranged from 284–316 for indications, 29–44 for short range, 46–83 for trend, 3–7 for breakout, 128–152 for reversion, 238–255 for flow, and 244–258 for multi. This is a functionality check, not a comparison of profitability. Positive and negative runs took about 67 and 77 seconds respectively, with peak process memory near 60 MB.

## Current execution status

The follow-up audit separates paper and actual evidence, enables per-Set/lane Axis and Set Block, and fixes fee and partial-fill accounting. It includes a read-only exchange commission check; no real-money order or engine start was performed. See the linked audit for current tests, sizing limits, and remaining execution uncertainty.

Selected in-sample PF is not a guarantee of future or executed PF. No result is clamped to a profitability target.
