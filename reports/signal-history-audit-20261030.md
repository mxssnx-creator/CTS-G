# Signal and history checkpoint — 30 October 2026

This is an unfinished checkpoint. It does not implement the CTS-A-O Signal-source engine or qualify it for live use. CTS-A-O `c32c32b19cc7ef97baea1e26e8b987ba8e7f50dd` has 103 source families and uses distinct source, range, timeframe, symbol, direction, strategy and protection identities. CTS-G's current Set scoring combines historical rows across symbols and sides. Adding source votes to that shared tape would not preserve the requested independent Signal and Exit qualification.

The CTS-A-O source's selected Signal, coordination, history and Exit test files passed 146 tests. Those are source-project tests, not evidence that CTS-G has imported those capabilities. The earlier CTS-G draft PR #116 is separate and retains its documented exchange-ownership and trailing-evidence blockers; it is not included in this branch.

## Changes in this checkpoint

- Set history can be configured and collected up to 4,320 completed one-minute bars (72 hours). The default initial lookback remains 1,920 bars, the default maximum is 4,320, and the default separate collection target is 12 closes. That target does not change the admission gate's minimum, PF or cost.
- Insufficient or zero-close scored Sets can grow history even when the admission minimum is off. Growth is bounded. While a longer window is being rebuilt, the Set gate waits until each usable history symbol has been replayed at the same lookback and topology; the desk shows that state. Open positions still need their own management. This can pause new Set entries during the rebuild and has not been soaked with a changing live universe.
- History paging now fetches added bars even when the old window is at least 90% of the new target. During a growth rebuild, failed or missing exchange pages must be fetched successfully before that symbol is counted as replayed at the new length; an actually short listing is accepted as short. Invalid candle prices cannot silently remove a minute and shift the timestamps of older bars. Held symbols remain in the history universe, and history chunks rotate fairly even when many positions are open.
- A trailing replay checks the stop already in force against a candle before using that candle's favourable extreme. It may use a newly tightened stop in the same candle only when the closing price proves a crossing after the extreme. The model still cannot recover unknown intrabar tick order from OHLC data.
- The Set replay and live Set exit now read the configured scratch-profit threshold, in percent, instead of always using 0.16%. Changing it invalidates old Set history. Focused long and short tests check that the historical and live lanes react to the same threshold.

## Frozen historical comparison

The input is the checked-in CTS-A-O Signals coverage candle archive, SHA-256 `e21d39b0b7583685e2fe355f6261e0860deff3057ca5c81b43a6c3cf374e2f03`. Eight symbols (ADA, AVAX, BCH, BNB, BTC, DOGE, DOT, ETH) use contiguous one-minute bars. The forward window is 17 October 2026, 09:00–21:00 UTC, with one extra hour of indicator warmup. Model cost is 0.12% per round trip. Each run uses 1,654 Sets, hourly sampled reports, per-entry gate evaluation and automatic holds disabled. This is a limited offline model, not live execution or a full-default test.

| Prior history | Base validated | Base PF | All-stage validated | Base-tape PF of all-stage passing Sets | Modeled closes | Modeled execution PF |
|---|---:|---:|---:|---:|---:|---:|
| 12 h | 0% at all 12 hourly checkpoints | unavailable | 0% at hourly checkpoints | unavailable | 3 | no losses; insufficient sample |
| 72 h | 0.36% average over 12 hourly checkpoints | 1.694 pooled over those checkpoints | 0.17% average | 1.639 | 16 | 0.031 |

The 12-hour run had one Set pass between the hourly checkpoints at 20:35 UTC: Base PF 1.557 and 1/1,654 Sets (0.061%); three positions closed before the end. The absence of hourly passing samples is therefore not a claim that no Set ever passed. The 72-hour sample includes repeated Set windows at successive checkpoints; these are not independent unique observations. Its next-eight pooled paper PF was 0.322 across 272 observations, some repeated by checkpoint. The 16 modeled closes were 2 long and 14 short. An independent fixed-exit walk matched all 16 model exit prices/reasons and single 0.12% cost deductions; no trailing Set executed in that window. These checks do not prove exchange fill parity or that the PF decline is free of other defects.

The execution model also left four positions open at 21:00 UTC. Marking them at the last candle close with the full 0.12% round-trip cost gives a summed per-position return of −4.50% for those four, and a combined closed-plus-marked PF of 0.023. This is a hypothetical equal-notional mark, not account equity or realized exchange performance. The baseline's 0.031 PF in the table includes only completed closes.

The 72-hour stage audit reads each passing stage's own processed tape. Across the hourly checkpoints, Main passed in 0.040% of Set observations with pooled PF 11.14; the positive Real lane had no passing observations; Real Negative passed in 0.171% with pooled PF 85.84. That last ratio is dominated by a loss denominator of only 0.0084 summed return: 32 of its 34 passing Set-checkpoint observations occurred at the first hour with no losses. Sets and underlying trades overlap, and checkpoints can repeat them; these figures are not 498 independent trades or evidence of live performance. This highlights the gap to the 16 modeled execution closes rather than proving the cause of the gap.

At every one of the 16 modeled entries, the same Set's own symbol-and-direction history was below the Base PF floor of 1.5: context PF ranged from 0.322 to 1.374 on 20–50 previous closes, while the combined tape PF ranged from 1.574 to 1.750. These 16 entries represent 16 distinct Set/symbol/direction contexts. For example, the first BTC short used a pooled PF of 1.750 but had a same-context PF of 1.079 on 20 closes. This exposes evidence shared across contexts in the existing General/Indications model; it is not a test of the absent CTS-A-O Signal implementation. Nor does it establish that adding a same-context gate would produce a profitable or sufficiently active strategy: that requires reranking and a new forward simulation.

## Exploratory separate-direction replay

The following offline experiments rebuild each historical Set's long and short shadow lane separately for each symbol, then run Base → Main → Real / Real Negative on that context's own tape. They compare the existing base, no-target Exit and trailing Set families together and separately, plus three exploratory time-stop/scratch configurations. Each experiment checks 48 indexed Set lanes against a bar-by-bar walk. The model still picks the strongest signal pack per symbol, permits one position per symbol, keeps automatic holds off and has no CTS-A-O Signal sources. These are exploratory tests on the same 12-hour window, not a sealed validation or live deployment model.

| Time stop / scratch | Average Base validated contexts | Pooled Base PF of passing contexts | Average all-stage validated contexts | Modeled closes / PF | Open at end |
|---|---:|---:|---:|---:|---:|
| 360 min / 10 min, 0.16% | 0.0069% | 1.564 | 0% | 0 / unavailable | 0 |
| 90 min / 10 min, 0.16% | 0.0060% | 1.568 | 0% | 0 / unavailable | 0 |
| 180 min / 20 min, 0.24% | 0.0255% | 1.616 | 0.0009% | 0 / unavailable | 0 |
| 360 min / 20 min, 0.24% | 0.0054% | 1.554 | 0.0006% | 1 / 0 | 0 |

The 360-minute / 0.24% trial's one close was an Exit Set loss of 1.59% of position notional after cost. The combined and each isolated Set-family trial produced the same absence of orders, except that one loss for the Exit family. Percentages use 8 symbols × 2 directions × 1,654 Sets × 12 checkpoints; overlapping windows and Sets are not independent observations. None of these settings qualifies as a best-performing or high-order configuration on this evidence.

Detailed data: [12-hour history](signal-history-12h-20261030.json.gz), [72-hour history](signal-history-72h-20261030.json.gz), [72-hour Base checkpoints](signal-history-72h-base-20261030.json), [stage audit](signal-history-72h-stages-20261030.json), [symbol-and-direction scope audit](signal-history-scope-20261030.json), [fixed-exit fill audit](signal-history-fill-audit-20261030.json), and the directional studies: [default](signal-history-directional-20261030.json), [90-minute stop](signal-history-directional-time90-20261030.json), [180-minute stop with later scratch](signal-history-directional-time180-20261030.json), and [later scratch](signal-history-directional-scratch24-20261030.json). The detailed simulator outputs are gzip-compressed JSON.

On the seeded replay fingerprint, base and Exit trade bytes matched the unchanged branch (77,223 and 32,247 rows). Trailing rows changed from 49,269 to 49,219 after the intentional intrabar correction. The 12-hour fixed-window execution output also matched the unchanged branch before the trailing correction; after the correction it still produced the same three modeled closes. A separate synthetic six-hour processing check covered 50 symbols × 103 Sets (5,150 Set–symbol lanes) in 316.75 seconds on this shared environment; it is not a live-cycle latency or memory benchmark. A further [single-symbol 72-hour replay](signal-history-benchmark-20261030.json) processed 1,654 Sets in 11.29 seconds with peak Linux RSS 103,104 KiB, while the process had loaded the eight input series. An [eight-symbol replay](signal-history-benchmark-eight-20261030.json) of 1,654 Sets over 72 hours completed in 77.99 seconds with peak Linux RSS 165,660 KiB. The shared host and disabled automatic holds make these narrow measurements, not full live-cycle capacity results.

After the fixes, the [full offline engine regression rerun](engine-regression-20261030.json) passed all 422 checks. Three older fixtures were adjusted to test the requested trailing or pre-history behavior independently. The JavaScript suite passed 191 checks with four skips; the TypeScript suite passed 73 checks. Type checking and a local production build passed. The Settings page rendered at desktop and mobile sizes. A separate [mocked-browser test](settings-browser-reference-20261030.json) saved and reloaded 4,320-bar lookback and maximum, a 24-sample target and 0.30% scratch threshold, and applied the captured values to an isolated SetBook. It made no live Pulse API calls and does not verify a real engine reload.

## Reproduction

With the referenced CTS-A-O archive available locally, use empty scratch paths. The cache must be generated by the same CTS-G code and must be trusted, since it is a Python pickle.

```sh
python3 scripts/prepare_signal_reference.py --feed /path/to/CTS-A-O/scripts/studies/signals-coverage/feed.json.gz --out /tmp/ctsg-feed72 --symbols 8 --pre-hours 72 --forward-hours 12
python3 scripts/trade_sim.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --symbols 8 --jobs 2 --last-hours 12 --eval-every 3600 --next 15,25 --patch '{"histLookbackBars":4320,"histLookbackAuto":false,"holdEnabled":false}' --out /tmp/ctsg-sim72
python3 scripts/audit_reference_stages.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json
python3 scripts/audit_reference_scope.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json
python3 scripts/audit_reference_fills.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json
python3 scripts/benchmark_reference_history.py --data /tmp/ctsg-feed72 --result /tmp/ctsg-sim72.json --symbol ADA-USDT
python3 scripts/benchmark_reference_history.py --data /tmp/ctsg-feed72 --result /tmp/ctsg-sim72.json --symbols 8
python3 scripts/study_directional_history.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json
python3 scripts/study_directional_history.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json --time-stop-min 90
python3 scripts/study_directional_history.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json --time-stop-min 180 --scratch-after-min 20 --scratch-min-pct 0.24
python3 scripts/study_directional_history.py --data /tmp/ctsg-feed72 --cache /tmp/ctsg-shadow72.pkl --result /tmp/ctsg-sim72.json --scratch-after-min 20 --scratch-min-pct 0.24
```

The fill audit explicitly reports trailing fills as unaudited. It assumes the above fixed settings and does not validate leverage, capital constraints, real exchange fills, Signal-source qualification or live order ownership. Independent Signal-source/strategy contexts, a separate Signals history setting and adaptive history, additional researched Exit tactics, broader forward tests, full live-parity/performance verification and safe integration with the separate ongoing draft remain open. No merge or live deployment is warranted on this evidence.

For the mocked browser test, start the local dev server on `127.0.0.1:3176` and run `node scripts/browser-settings-reference.mjs`. Its request handler intercepts config, statistics and connection endpoints and rejects non-local traffic; it never writes an account or a Pulse overlay.
