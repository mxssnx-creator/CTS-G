# Execution integrity and Low-Range audit — 7 November 2026

Starting branch: Stable-02-01, `70e07747c8f9c2c29f3d49d8f7e169070b043655`.
CTS-A-O source: `9f054c3`, `src/core/indications/low-range-research.ts`.

## Correctness changes

- Signal arbitration checks the signal's direction. A stronger LONG signal with only SHORT-qualified sets no longer masks another executable pack. Eligibility is refreshed even while the completed-bar signal is memoized.
- The offline executor falls through to the next signal when its preferred pack has no qualifying direction/lane/Axis/group candidate.
- Base validation statistics use the Base-passing population; Main/Real eligibility has its own percentage. Next-N evidence excludes positions entered before the validation decision.
- Block v2 add PF/net uses each filled count's actual ratio and carry scale. DCA replay interprets exposure cap zero as unlimited, matching the engine. Final-hour settlement remains included in hourly totals.
- 1,950 CTS-A-O Low-Range names are available as independent packs. The 15-name `presets/cts-a-o-low-ranges.patch.json` is an opt-in research patch, to append/merge with existing `indicationConfigs`; it is not a replacement account overlay. Each entry builds TP steps 2–12, SL ratios 0.5/1/1.5, base/Exit/trailing, and independent sides.

## Frozen market experiment

12 fixed symbols: BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX, LINK, DOT, BCH, LTC; all USDT.
Forward interval: 6 November 19:00 UTC to 7 November 19:00 UTC. 49 hours total data; 24-hour evaluated lookback and 24-hour forward report.
Public BingX taker fee 0.05% per leg plus slippage 10% of fees gives 0.11% modeled round-trip cost, charged once. This is a public current-fee estimate, not historical or account-specific commission. Funding and order-book fills are not modeled.

| Portfolio | Closed | Executed PF | Net/base notional | Closed + marked PF |
|---|---:|---:|---:|---:|
| Original, Axis off | 58 | 1.094 | +1.20% | see saved report |
| Entry fixes, Axis off | 63 | 1.904 | +8.15% | 1.151 |
| A-O additions, Axis off | 56 | 1.038 | +0.49% | 0.754 |
| A-O additions, current four Axis rules | 61 | 1.835 | +7.67% | 1.217 |

All final portfolios have 12 held positions. These figures are not account returns. The main expanded replay has 6,068 Sets, mean hourly Base pass share 11.14%, selected Base PF 1.696 and Main/Real eligible share 1.32%. Those are different populations from actual subsequent execution.

Independent current-Axis portfolios: base Micro PF 0.755/73 closes, Minimal 2.527/67, Short 1.069/53; Exit Micro 0/6, Minimal 0/2, Short 2.049/13; trailing Minimal 1.059/63 and Short 0.725/49. They are independent experiments, not additive positions. Micro trailing has zero feasible configured arms under the default distance floor. Do not mislabel these outcomes as every range being profitable.

Four last-N variants are compared on the preceding 12 hours, with the next 24 hours reported independently. That exploratory calibration does not establish a profitable training winner and must not replace live defaults. The original and every losing candidate remain in the report.

Public candle/indication readiness passed for 200/200 symbols, 720 complete candles each, all 15 imported signal functions. No private account authentication, live orders, execution reconciliation or production deployment was performed.

## Future change requirements

High historical PF cannot guarantee high future PF. Preserve the configured qualification floors, actual losses, tiny losses, unknown evidence and held positions. Never clamp PF upward, omit an admitted position's later loss, borrow a sibling's evidence, weaken samples to inflate counts, or fit a change to a claimed sealed evaluation.

For each execution-affecting change:

1. Reproduce the defect with a causal fixture, including both sides and independent pack/config/type/range/lane identities.
2. Verify the signal decision, parent selection, fees once, partial/add volume and close ownership. Replays must match the engine's gate/rank and protection rules.
3. Freeze source, market hashes, settings and chronological windows. Keep prehistory, selection PF, subsequent closed PF and open marks separate.
4. Report passed/evaluated counts and timestamps per stage, actual closed/open counts, drawdown and hourly outcomes. Keep zero/insufficient/losing cohorts visible.
5. Compare against the unchanged control. Promote an economic configuration only with further independent forward evidence; a passing code test or this one-day result is insufficient.

Regression coverage includes 390,000 direction decisions exported from the actual CTS-A-O source, registration and side/type isolation, live directional arbitration, actual SetLane replay fallback, future-entry evidence, cost/weight reconciliation and DCA cap semantics.

Reproduction tools: `fetch_audit_market.py`, `trade_sim.py --stage-details --axis-current`, `range_execution_audit.py`, `lowrange_window_audit.py`, `stack_eval.py --part axis,block2`, `market_smoke.py`, and `render_execution_audit.py`. Saved result/data archive includes the exact patches and all candidate outcomes. Market fetching produces a new snapshot; reproducing this experiment requires the archived snapshot.
