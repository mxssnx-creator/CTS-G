# Strategy controls, Tactic / Inverse and relational volume — 2026-11-11

This change applies to CTS-G, CTS-A-O, CTS-A, CTS-C, CTS-X and CTS-K-N. It adds settings and calculation/execution adapters; it does not deploy or start exchange trading.

## Processing controls

- Global Normal and Trailing switches combine with the exact coordination/range switch using AND. An omitted range setting inherits the global master.
- Normal off retains the calculation history needed by additional Axis, Block and DCA processing, but blocks standalone Normal and standalone Trailing execution. Each additional family still follows its own enablement and qualification.
- Trailing off blocks new trailing configurations and freezes future trailing adjustments on held positions. The last confirmed protective stop remains active. Fixed protective exits remain independent.
- Range identity comes from the actual lane, not another lane with a similar name. Signals and Exit retain their own scope where supported. Save/reload preserves explicit false values.

## Volume settings and defaults

The persisted `relationalVolumeRatios` policy contains `enabled`, `overall`, `maxMultiplier` and maps for `indicationTypes`, `indications`, `strategies`, `ranges`, `symbols`. Every omitted weight is 1.0. The new policy defaults to enabled with neutral weights and a ceiling of 25. Existing account sizing, margin limits, venue minimums, risk budgets and rounding still apply.

Each matching weight is multiplied once for a new entry. Repeated keys within the same bucket are deduplicated, including tier + geometry contexts. Weights are 0.1–5.0 in increments of 0.1; the combined ceiling is 1–25. The equalize button restores all weights to 1.0. Explicit type/range/strategy weights are separate from existing strategy adjustment ratios: changing this policy does not rewrite previous fills, unit net returns, or the evidence used for qualification.

Parent-sized Block/DCA adds inherit their parent's already sized notional and apply only their own new strategy weight. CTS-A's DCA notional is independently computed from the base budget, so it resolves the parent's source/range identity with the DCA strategy weight once. A Block parent does not apply the same Block weight a second time. Axis is an independent family rather than a Normal-weight multiplier.

| Project | Settings placement | Native coordination/range identity |
| --- | --- | --- |
| CTS-G | Indication / packs, Strategy / sets | micro, minimal, short, normal, long, wide, signals, exit |
| CTS-A-O | Indication focus, strategy processing / protection | wide, mc, mn, mp, sh, gn, lg, lr, sig, exit |
| CTS-A | Indications and strategies panels | normal/micro/minimal/short plus linear/geometric/atr/volume/fibonacci |
| CTS-C | Indication types and strategies / coordinations | native named ranges including Micro; persisted configuration schema |
| CTS-X | Indication focus, strategy processing / protection | wide, mc, mn, mp, sh, gn, lg, sig, exit |
| CTS-K-N | Settings → Indications and Strategy; connection coordination processing | main, preset, signals, direct |

## Sources and independence

The original CTS-A-O Inverse source implementation is preserved from revision `819b6621f25393e3fe66da4a0fa0a52c4baee709`. All 604 Inverse identities are pre-entry calculations. No return is sign-flipped after a trade. CTS-A-O and CTS-X use their native engine; the other four use the same source graph through bounded adapters. The current merged CTS-A-O also matches all 2,918 deterministic source-state hashes.

Tactic preserves the existing 10 sources and adds 2,304 independent decline/recovery identities: 4 methods (bounce, trend, vwap, sweep), 4 lookbacks (8/20/40/80), ATR/percent, spacing 0.5/1, levels 1–12, recovery 0.25/0.5/1. Original first-576 DCA identities/indices stay stable; deeper levels are additive. Closed-bar feature scans are shared per lookback, avoiding one rescan per level/ratio. Realtime incremental calculations match full-prefix calculations.

Inputs must be genuine closed, contiguous OHLCV bars with original timestamps. Higher frames use only completed UTC-aligned candles. Inverse uses its original 1m/5m/15m frame rules; unsupported 30m Inverse does not invent a fallback. Pair and market sources require causal matched peer bars. Missing peers, stale data, gaps and unsupported sources fail closed. CTS-C currently supplies only its own symbol stream to the portable adapter, so peer-dependent sources remain unavailable there. CTS-G batches selected source calculation off the live control loop, with a bounded pending queue and 32 MiB cache. CTS-A obtains closed candles separately from its fast ticker/control loop and isolates network/connection caches.

New source selections are opt-in and still pass the project's own stages. Bulk enabling thousands of lanes without resource and sample qualification is not part of this change. Portable source graphs carry SHA-256 manifests and exact source IDs; no pseudo-price signal is used in place of the original Inverse calculation.

## Verification and economic limits

Run `npm run test:strategy-features` in each repository (pnpm also supported in CTS-K-N), then the existing typecheck and build commands. The new GitHub workflow runs these gates on pull requests. CTS-C additionally runs its isolated browser settings/save/reload test. The browser test uses a temporary store, blank exchange credentials and denied private venue writes.

Local builds passed for all six projects. CTS-C's full native suite passed 195 tests. CTS-G passed its earlier complete 705-check regression before integration of the latest upstream Block fixes; the integrated run is recorded separately. New feature tests cover range/master truth tables, extra-family independence, held-stop freezing, source parity, causal/gap/reference behavior, sizing and persistence. This is scoped verification, not a claim that every historical test in every project or authenticated venue lifecycle passed.

The historical report covers 87,540 matrix cells on BCH, then XRP and SOL, over 14 complete UTC days (2026-10-28 to 2026-11-11 exclusive), at 1m and 5m, plus warmup. All matrix cells, frozen public market inputs and checksums are in the CTS-A-O evidence directory. Chronological validation uses the later seven days, which are already examined historical dates and must not be called an unseen holdout. The research gate exercise is independent from each project's complete production portfolio.

Cost assumption is 0.08% taker per side plus 10% of fees as slippage, round trip 0.176%; stress 0.264% applies to the frozen shortlist. This is a stated assumption, not an authenticated fee snapshot. There are positive individual validation configurations, but the training-only shortlist produces zero modeled Live closes after own last-N 30→8→8 gates. High PF through execution, higher live order counts and live profitability are therefore **not established**. Neutral weights are retained; qualification floors are not weakened to manufacture results.

See [interactive report](strategy-features-20261111/report.html), [results](strategy-features-20261111/results.json), and [full matrix / input evidence](https://github.com/mxssnx-creator/CTS-A-O/tree/main/docs/verification/strategy-features-20261111).

## Research basis

- Leung & Li, [Optimal Mean Reversion Trading with Transaction Costs and Stop-Loss Exit](https://arxiv.org/abs/1411.5062): entry/exit distances depend jointly on costs and stops.
- Martin & Schöneborn, [Mean Reversion Pays, but Costs](https://arxiv.org/abs/1103.4934): transaction-cost buffers motivate selective decline/recovery thresholds.
- Kitron & Wengrowicz, [Short-horizon mean reversion in cryptocurrency markets: a matched cross-market measurement](https://arxiv.org/abs/2608.21888): detectable gross reversal is not sufficient evidence of net tradability.
- Moreira & Muir, [Volatility-Managed Portfolios](https://www.nber.org/papers/w22208), and [CME position sizing](https://www.cmegroup.com/education/courses/trade-and-risk-management/proper-position-size.html): motivate risk-aware relative sizing, not a promise that a specific CTS ratio is optimal.

Application to CTS is an implementation/research inference. The papers do not validate CTS profitability.
