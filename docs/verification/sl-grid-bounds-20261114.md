# Preserve empty bounded SL grids — 14 November 2026

When all globally configured SL ratios were excluded by `setSlMinRatio` or `maxStopLossRatio`, SetBook restored the original unfiltered ratios. A configuration with only ratio 2.0 and a maximum 0.5 therefore still built 12 base/Exit cells at 2.0 in the bounded reproduction.

Keep the filtered global grid empty when none of its ratios is allowed. Do not replace it with defaults or re-enable rejected ratios. Explicit per-range geometries retain their declared independent bounds and populate only their own ranges. Their documented overrides are unchanged; legacy trailing geometry is also unchanged.

The existing minimum/maximum grid regressions now exercise a wholly excluded grid, contradictory bounds, ordinary partially filtered grids and explicit range isolation after an empty global grid. No shipped overlay, fee, PF floor, Last-N requirement, position or service is changed. This source correction does not establish a profitable forward/live cohort.
