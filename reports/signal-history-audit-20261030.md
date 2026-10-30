# Signal and history checkpoint — 30 October 2026

This is an unfinished checkpoint. It does not implement the CTS-A-O Signal-source engine or qualify it for live use. CTS-A-O `c32c32b19cc7ef97baea1e26e8b987ba8e7f50dd` has 103 source families and uses distinct source, range, timeframe, symbol, direction, strategy and protection identities. CTS-G's current Set scoring combines historical rows across symbols and sides. Adding source votes to that shared tape would not preserve the requested independent Signal and Exit qualification.

The CTS-A-O source's selected Signal, coordination, history and Exit test files passed 146 tests. Those are source-project tests, not evidence that CTS-G has imported those capabilities. The earlier CTS-G draft PR #116 is separate and retains its documented exchange-ownership and trailing-evidence blockers; it is not included in this branch.

## Changes in this checkpoint

- Set history can be configured and collected up to 4,320 completed one-minute bars (72 hours). The default initial lookback remains 1,920 bars, the default maximum is 4,320, and the default separate collection target is 12 closes. That target does not change the admission gate's minimum, PF or cost.
- Insufficient or zero-close scored Sets can grow history even when the admission minimum is off. Growth is bounded. While a longer window is being rebuilt, the Set gate waits until each usable history symbol has been replayed at the same lookback and topology; the desk shows that state. Open positions still need their own management. This can pause new Set entries during the rebuild and has not been soaked with a changing live universe.
- History paging now fetches added bars even when the old window is at least 90% of the new target. Held symbols remain in the history universe, and history chunks rotate fairly even when many positions are open.
- A trailing replay checks the stop already in force against a candle before using that candle's favourable extreme. It may use a newly tightened stop in the same candle only when the closing price proves a crossing after the extreme. The model still cannot recover unknown intrabar tick order from OHLC data.

## Frozen historical comparison

The input is the checked-in CTS-A-O Signals coverage candle archive, SHA-256 `e21d39b0b7583685e2fe355f6261e0860deff3057ca5c81b43a6c3cf374e2f03`. Eight symbols (ADA, AVAX, BCH, BNB, BTC, DOGE, DOT, ETH) use contiguous one-minute bars. The forward window is 17 October 2026, 09:00–21:00 UTC, with one extra hour of indicator warmup. Model cost is 0.12% per round trip. Each run uses 1,654 Sets, hourly sampled reports, per-entry gate evaluation and automatic holds disabled. This is a limited offline model, not live execution or a full-default test.

| Prior history | Base validated | Base PF | All-stage validated | Base-tape PF of all-stage passing Sets | Modeled closes | Modeled execution PF |
|---|---:|---:|---:|---:|---:|---:|
| 12 h | 0% at all 12 hourly checkpoints | unavailable | 0% at hourly checkpoints | unavailable | 3 | no losses; insufficient sample |
| 72 h | 0.36% average over 12 hourly checkpoints | 1.694 pooled over those checkpoints | 0.17% average | 1.639 | 16 | 0.031 |

The 12-hour run had one Set pass between the hourly checkpoints at 20:35 UTC: Base PF 1.557 and 1/1,654 Sets (0.061%); three positions closed before the end. The absence of hourly passing samples is therefore not a claim that no Set ever passed. The 72-hour sample includes repeated Set windows at successive checkpoints; these are not independent unique observations. Its next-eight pooled paper PF was 0.322 across 272 observations, some repeated by checkpoint. The 16 modeled closes were 2 long and 14 short. An independent fixed-exit walk matched all 16 model exit prices/reasons and single 0.12% cost deductions; no trailing Set executed in that window. These checks do not prove exchange fill parity or that the PF decline is free of other defects.

The 72-hour stage audit reads each passing stage's own processed tape. Across the hourly checkpoints, Main passed in 0.040% of Set observations with pooled PF 11.14; the positive Real lane had no passing observations; Real Negative passed in 0.171% with pooled PF 85.84. That last ratio is dominated by a loss denominator of only 0.0084 summed return: 32 of its 34 passing Set-checkpoint observations occurred at the first hour with no losses. Sets and underlying trades overlap, and checkpoints can repeat them; these figures are not 498 independent trades or evidence of live performance. This highlights the gap to the 16 modeled execution closes rather than proving the cause of the gap.

Detailed data: [12-hour history](signal-history-12h-20261030.json.gz), [72-hour history](signal-history-72h-20261030.json.gz), [72-hour Base checkpoints](signal-history-72h-base-20261030.json), [stage audit](signal-history-72h-stages-20261030.json), and [fixed-exit fill audit](signal-history-fill-audit-20261030.json). The detailed simulator outputs are gzip-compressed JSON.

On the seeded replay fingerprint, base and Exit trade bytes matched the unchanged branch (77,223 and 32,247 rows). Trailing rows changed from 49,269 to 49,219 after the intentional intrabar correction. The 12-hour fixed-window execution output also matched the unchanged branch before the trailing correction; after the correction it still produced the same three modeled closes. A separate synthetic six-hour processing check covered 50 symbols × 103 Sets (5,150 Set–symbol lanes) in 316.75 seconds on this shared environment; it is not a live-cycle latency or memory benchmark.

The full offline regression run produced 416 passes and three failures in fixtures that assumed the old same-candle trailing or pre-history behavior. Those fixtures were adjusted to test the requested rules independently, and all three passed when rerun. A full post-adjustment rerun is not claimed here. Type checking and a local production build passed. The Settings page rendered at desktop and mobile sizes, but the local Pulse statistics API was absent and the browser automation client failed to start; interactive saving and engine application were not verified.

## Reproduction

With the referenced CTS-A-O archive available locally, use empty scratch paths. The cache must be generated by the same CTS-G code and must be trusted, since it is a Python pickle.

```sh
python3 scripts/prepare_signal_reference.py --feed /path/to/CTS-A-O/scripts/studies/signals-coverage/feed.json.gz --out /tmp/ctsg-feed72 --symbols 8 --pre-hours 72 --forward-hours 12
python3 scripts/trade_sim.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --symbols 8 --jobs 2 --last-hours 12 --eval-every 3600 --next 15,25 --patch '{"histLookbackBars":4320,"histLookbackAuto":false,"holdEnabled":false}' --out /tmp/ctsg-sim72
python3 scripts/audit_reference_stages.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json
python3 scripts/audit_reference_fills.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json
```

The fill audit explicitly reports trailing fills as unaudited. It assumes the above fixed settings and does not validate leverage, capital constraints, real exchange fills, Signal-source qualification or live order ownership. Independent Signal-source/strategy contexts, a separate Signals history setting and adaptive history, additional researched Exit tactics, broader forward tests, full live-parity/performance verification and safe integration with the separate ongoing draft remain open. No merge or live deployment is warranted on this evidence.
