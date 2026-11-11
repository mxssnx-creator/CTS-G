# Preserve the executed-group gate on alternate lanes

When pack coordination blocked the initially selected lane, `_scope_choice` searched an allowed alternate lane without the executed-group predicate. With `groupGate` enabled, a losing execution group could therefore be selected after the initial search had applied the gate correctly.

The fallback now retains the same group predicate and direction restriction. The regression constructs a blocked positive scope, a losing negative group and a healthy alternative negative group. It checks selection of the healthy group, rejection when all alternatives lose, and unchanged behavior when the optional group gate is off. The installer pin and recovery patch are regenerated from the same source.

This does not enable the optional group gate, change economic defaults, or change the one-parent-per-symbol execution model. All 123 trader checks and all 640 offline engine checks pass (477 seconds with four workers). A filter prevents a specific bypass; it cannot guarantee future profitability.

## Frozen market replay

The saved 12-symbol, 24-hour window (6 November 19:00 UTC to 7 November 19:00 UTC, with 24 hours of prior evaluation history) was rebuilt with current source and a new provenance-checked cache. It reproduced the earlier core low-step run exactly: 1,210 sets, 58 closes, return PF 1.206, summed net/base notional +2.20 percentage points, 12 held positions and closed-plus-marked PF 0.778. Every trade record equals the control. This is an OHLC model, not venue fills or account return; the optional group gate is off in this reference configuration.

At the final 7 November 18:00 UTC evaluation, Base passed 300/1,210 (24.79%), with selected-window PF 1.660; 41/1,210 (3.39%) were executable. The Real positive window passed 24/75 with selected PF 2.987 and Real negative 20/53 with selected PF 2.866. These are overlapping selection populations and must not be substituted for the subsequent execution PF. The report includes hourly settlements, all trades and all stage samples. No exchange session was started.
