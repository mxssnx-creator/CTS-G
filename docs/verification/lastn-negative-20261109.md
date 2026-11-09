# Expanded Last-N and negative filters — 2026-11-09

Requested scope: independent multi-window qualification through exact symbol/config/type lanes, with lower positive windows and additional negative windows. No future-profit guarantee is asserted.

## Native CTS-G market sensitivity

150 trials: Micro / Minimal / Short × 10 Base/Main/Real tuples × positive, negative recovery 1/3, negative recovery 5/10, recent-loss veto 1, recent-loss veto 3+5. Twelve frozen symbols; 24h evaluated prehistory and 24h trading (2026-11-07 06:00 through 2026-11-08 06:00 UTC). Completed-minute input; four indication configurations per range; normal base TP/SL only; independent symbol/config identities; SL/TP 0.25 and 0.5. Existing Axis stays enabled. Block/DCA/Relate are disabled in this comparison. One physical position per symbol remains a shared execution constraint.

Strict full windows; Base secondary and Main-short selection disabled to isolate the tested policies. Public frozen maker 0.02%, taker 0.05%, fee-relative slippage 10%; the market/market modeled roundtrip is 0.11%. Funding is excluded. This is reused retrospective research data, not an independent holdout or verified account execution.

| Range | Strongest PF with >=10 total closes | Closed parents | Base/Main/Real | Mode | First half PF / n | Second half PF / n |
|---|---:|---:|---|---|---|---|
| Micro | 0.559702 | 42 | 5/3/1 | positive | 0.489583 / 18 | 0.619885 / 24 |
| Minimal | 1.256684 | 10 | 10/5/3 | veto recent loss 1 | no-loss / 1 | 1.048128 / 9 |
| Short | 1.032244 | 17 | 10/5/3 | veto recent loss 1 | 0.984006 / 7 | 1.064449 / 10 |

**0/150 passes** the predeclared screen: >=10 settled parents and PF>1 in each chronological 12h half. No economic preset is promoted. Tiny or empty high-PF cells are retained in the report but cannot establish an edge. New losing outcomes are never removed because their source later fails qualification.

Independent price arithmetic checks all 7,962 trade occurrences, costs exactly once, 3,600 hourly windows, drawdown, stage count bounds and chronological halves: no mismatches. Six hundred sampled engine/replay verdict comparisons: zero mismatches. Cross-project recent-loss arithmetic: 2,880 comparisons, zero differences. Repeated trade occurrences across trials are not independent market observations.

Reproduce with `scripts/lastn_sensitivity.py` and `scripts/report_lastn_sensitivity.py` in CTS-G. The evidence archive includes all trial JSON, both protocols, frozen input candles, code and fingerprints. The earlier 12-case independent-lane study predates the strict Base-full-window fix and is superseded as comparative market evidence. Later global lower-bound fixes do not alter this sweep's explicit per-profile windows.

## Negative semantics and preservation

Recent-loss veto: optional 1–200 completed own-parent observations, up to 16 windows, any/all mode, max loss PF 0–1. Inputs are already net of costs. Insufficient/nonfinite evidence cannot pass. Tiny losses remain losses. The veto changes later admissions only, never held ownership, exits or historic performance. Same ID with a changed economic policy is a new qualification identity.

CTS-G's existing negative recovery is a same-direction branch with its own positive Real-negative validation. It is not an inverse strategy. CTS-A-O's requested opposite-direction processed Main remains a separate pending runtime integration.

## Earlier demo run, not acceptance of this patch

CTS-G x02 VST ran approximately 20m25s before network policy blocked the venue. Cleanup confirmed zero positions and zero orders. Twenty-two entries and twenty-two exits: one natural close, twenty-one interruption cleanup closes. All-owned-close return PF 0.230211; cash PF 0.226618; cash net -0.127680 VST. This predates the new policies and is not a complete 30-minute run. CTS-A and CTS-A-O 30-minute runs were not completed. No new x01 run, no deployment, and no live/sim parity claim without matching final candles.

## CTS-G implementation and validation

- Global, per-range and per-profile Base/Main N now preserve 1–200 instead of silently clamping low values to 10. Current global defaults remain unchanged.
- Up to eight independent policy profiles per indication config, with immutable economic identity and optional explicit symbols; scope/role labels do not permit borrowing a sibling's closes. Expansion is bounded; omitted overflow is reported.
- Strict full-window Base now needs its full configured N in native engine and replay. Available-window mode remains available. Main/Real loss vetoes read their own predecessor tapes.
- Per-step and SL PF maps, settings editor and config roundtrip agree. No-loss, unknown and empty samples remain distinct.
- Relate is ported with 12-step default, <=0.5 effective SL/TP, bounded exact ownership/actual funded portions and persisted entry intents. It stays disabled: no profitable native Relate preset has been established.
- Connection-scoped VST REST/WS identity, bounded transport waits and dynamic-universe refresh are retained.

Local full run before final low-bound edits: 667/670 passed; three stale strict-Base fixtures failed. All were corrected to require the actual primary window rather than weakening production. Final focused gates 20/20, policy 6/6, Relate 12/12, engine self-test 84/84 and changed processing check 1/1 pass. TypeScript, typecheck and build pass. The publication PR runs the complete final tree again; do not label the earlier full run as all-green. Browser acceptance remains unavailable locally.
