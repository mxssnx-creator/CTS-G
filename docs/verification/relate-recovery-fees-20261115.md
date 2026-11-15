# Relate restart fill and fee integrity — 2026-11-15

Base: `9833e696a10de4825ea4119dfac4dc4aabf1425c` on `Stable-02-01`. No account/service settings or credentials were changed, and no exchange writes were made.

## Reproduced defects

`recover_relate_intent` accepted an order by matching its client ID and quantity, without requiring the order's symbol, position side and entry side. Its `filled_qty` fallback could use the requested/original quantity when the venue had not reported a fill. Malformed or nonfinite average prices could be accepted or abort reconciliation. The two new behavioral checks fail on the original production code: 9 of 14 adverse scopes are not safely rejected, and all four long/short fee-recovery cases fail.

A recovered confirmed entry also called `fill_cost` without its Position, losing the identity-bound entry fee leg. It assigned internal `taker-rate` rather than the canonical `exchange-rate-estimate` label. Its order ID and actual fill average were not retained on a newly recovered probe. Finally, the newer recovery hold clock could exclude the original entry commission from the fee query.

## Change

Recovery requires an owned pending client ID, the exact returned order identity, an actual reported positive executed quantity, a usable order ID and a finite positive fill average. Missing/foreign/malformed evidence stays unknown without mutating the Position, saving it or releasing its pending intent. Valid recovery retains the confirmed order ID, entry average/notional and base quantity, records the fee leg on the Position and canonical fee provenance, and persists the original entry fee-query timestamp independently of its hold clock. Complete fee reconciliation may promote estimates only when every entry/exit leg is covered; explicit reported zero fees remain zero. The re-pinned trader is rebuilt by the existing restore helper.

The saved Secure Minimal JSON also lacked the three Auto SL range fields recently added to the Settings factory. The existing preset-equality test failed with exactly `slAutoRange`, `slAutoMin`, `slAutoTol` missing. The JSON now carries the same requested defaults (true / 20 / 5). Its conservative sizing and enabled strategies remain as defined by the preset.

While the PR was under verification, target commit `a4c2b3e757ec35e588d4bf878cd38c0523f81e11` added `trailStepMin`. The target is integrated without reverting its eight changed files, including the x02 source preference of 150 symbols. The preset now also contains `trailStepMin: 3`, matching the Settings factory; no active account setting was applied. The first hosted verify attempt caught this fourth preset-field drift and is retained as a failed attempt rather than counted as a passing gate.

## Validation

- Original target: full 736-check regression, 0 failures/skips, 248 s.
- Before runtime correction: both new recovery checks fail on unchanged production code; the failing output is retained in the continuation evidence.
- Corrected focused run: all 14 Relate checks pass, including 18 adverse order/payload scopes and four long/short rate/reported-zero restart cases.
- Existing fee evidence tests: 7 pass; costs: 21 pass; logistics: 30 pass, including GNU patch and installer git-apply reconstruction.
- JavaScript tests: 191 passed and 4 explicit skips.
- Typecheck and development production build pass.
- TypeScript tests: 125/125 pass after synchronizing the saved preset.
- Five exact planted faults are caught by the new behavioral checks (requested quantity, foreign scope, missing fee leg, newer-clock commission boundary, nonfinite price); zero survivors or missing anchors. The first boundary-mutation attempt revealed that a late import could load the original fee helper under the mutation harness. Fee helpers are now imported with the trader, and the test calls the actual trader binding. The final isolated fault checks fail for the intended behavioral reason.
- A final full core run and hosted CI are recorded in the PR and all-project continuation report after completion; this document does not predeclare those runs green.

## Limits

This repairs evidence handling, not the signal's market edge. There is no new post-Base/forward/venue PF or twelve-symbol funded acceptance. A same-account netted position containing unrelated activity still cannot be assigned to this engine merely from a position snapshot. Bybit's separate commission API integration remains outside this change; a fee leg that cannot be reconciled remains an estimate.

