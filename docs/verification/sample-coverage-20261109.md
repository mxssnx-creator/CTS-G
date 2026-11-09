# Last-N sample coverage and qualification repairs — 9 November 2026

Sample sufficiency is reported separately from PF validation. Full losing samples count as sampled; empty contexts remain in the denominator. No research preset is promoted and no live account is started by this change.

## Changes

Each configured Set records its eligible own Base, Main, Real and Real-negative close counts at its scored clock. The existing per-window table adds full/population and percentage; disabled and unmeasured states remain distinct. Main/Real denominators use their predecessor-qualified populations. Negative recovery coverage requires all configured dip windows (maximum N); Main short uses the chosen N. Economic changes invalidate old counts.

Two bugs were reproduced. Base Last-1 was slicing the tape before an independent minimal Last-3 condition, causing false insufficiency and, under partial-window rules, hiding older observations. Native scoring and stage_eval now read each window separately from the same eligible history. A per-range Base-short window also displayed zero passing Sets when its global counterpart was off; the report now respects the range setting. The stale regression expecting configured Real 8/3 to clamp to 10 is corrected to the requested 8/3.

## Verification

The preceding coverage-only checkpoint passed 673 Python regressions, 107 TypeScript tests, 191 Node tests (4 unrelated skips), 109 engine checks, typecheck and production build. Both independent-window reproductions pass after the follow-up. The full final suite is recorded in the release PR/checks. Actual table server rendering passes with explicit unavailable/disabled and 2/4 (50%) fixture states. Local Chromium visual inspection is unavailable because its process-singleton socket is denied by the host.

## Financial evidence

No new current-policy market run is claimed. The earlier 150-case Last-N/negative sensitivity screen found zero cases with PF > 1 and at least ten settlements in both chronological 12-hour halves. Those results predate this fix. Current post-Base PF, validated percentage and venue execution PF are unavailable for this source. Sample percentages require a newly scored snapshot and must not be inferred from old PF statistics.
