# Current-target audits of older CTS-G fee/replay drafts

The target was `Stable-02-01` at `9833e696a10de4825ea4119dfac4dc4aabf1425c` (tree `85ef659b47269ea6794aca43aa332020465c73eb`). The complete fresh target regression passed 736 checks with no failures/skips in 248 seconds. These are code-integration audits, not a new funded or positive-PF acceptance.

## Draft #121



### Current-target audit — 2026-11-15

Audited the unchanged draft head `f6c10a927299b2dbf2d4a5c94b94425fad3d0769` against CTS-G `Stable-02-01` at `9833e696a10de4825ea4119dfac4dc4aabf1425c`. This closes the superseded draft without merging its stale branch; the branch is retained.

| Draft claim | Current implementation / evidence |
| --- | --- |
| Active contracts only in shared paper budget | `Pulse.active_fee_contracts`, `refresh_fees`, `apply_live_config`; inactive-contract regression passes. |
| Numeric/string zero commission responses | `FeeSchedule.refresh` normalizes the response code; account and symbol scopes remain separate. |
| Actual fill notionals; fee-only fallback; no double slippage | `fill_cost` now uses explicit `fee_policy` maker/taker rates, replacing the older configured-total-minus-allowance fallback. Per-leg math, both directions, mixed liquidity, reported zero and partial/restart checks pass. |
| Least-certain fee provenance retained | `note_fee_source` is used for exits and Block/DCA adds; immutable net figures survive cost-policy changes. |
| Correct Block/DCA cash fixtures | Current `correctness` / `lanes` suites use each leg's fill notional and the current explicit fee policy. |
| Deployable engine restore | Current trader pin `192bd9b975` reproduced from the verified base blob by both GNU patch and the installer's git-apply path. The earlier draft pin is intentionally superseded. |
| Report hygiene | Saved report exclusions retained and complete regression passes. |

Fresh offline validation on that target: **736 passed, 0 failed, 0 skipped, 248 s**. Focused costs: **21/21**; restore: **1/1**. No orders, account writes or live profitability acceptance are claimed. Funding attribution and full paired venue/forward acceptance remain separate work.

## Draft #120



### Current-target audit — 2026-11-15

Audited the unchanged draft head `d9ff67e31b097844638246c11e811ebf47b19f73` against CTS-G `Stable-02-01` at `9833e696a10de4825ea4119dfac4dc4aabf1425c`. This closes the superseded draft without merging its stale branch; the branch is retained.

| Draft claim | Current implementation / evidence |
| --- | --- |
| Do not close positions with post-window candles | `stage_eval.execute` bounds scans by the requested window's end. |
| Keep unclosed long/short positions visible and occupying their symbol | `include_open`, `busy`, `open_at_end` and mark returns are retained; the current implementation also reserves the continuation slot for unfinished positions. |
| Distinguish settled and marked results | `trade_sim.py` exports both `open_at_end` and `closed_and_marked_pf`; settled results remain separate. |
| Reproduce the recorded simulation cost | All six reference/directional audit helpers copy `sim["config"]["cost_pct"]` into the replay patch. |
| Preserve historical evidence | Both `reference-cost-window-20261030.json` and `.md` are byte-identical Git blobs (`1fe3a9396b4c13c45da04cea0c8426477c6300f8`, `854b14bda7812b439f95b0be69ba75c42f39473c`). Their historical results are not re-labelled as current acceptance. |
| Report hygiene | Saved regression-report exclusion retained. |

Fresh target regression: **736 passed, 0 failed, 0 skipped, 248 s**. The focused window-end oracle passes with **two open positions (long/short), zero invented closes, and a stop-reaching candle outside the requested window ignored**. No funded/live PF claim is made.

Both drafts were individually re-read at their exact recorded heads immediately before closure. Neither old branch was merged or deleted. The historical low/negative PF figures remain unchanged in their reports.

