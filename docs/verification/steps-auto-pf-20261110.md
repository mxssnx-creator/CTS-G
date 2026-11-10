# Step and automatic PF audit — 10 November 2026

Three native/replay defects and one display defect were reproduced against the
previous implementation and repaired:

- Replay Auto PF fitted on closes inside the very lookback it was judging.
  Native replay learns strictly before `decision time - lookback`. The simulator
  now uses that same cutoff at each hourly refresh. With only current-window
  evidence, both retain the manual fallback rather than fit on the tested window.
- Configured steps 36–50 looked up missing Auto table entries and fell back to
  manual PF, although the defined table ends at step 35. They now share bucket
  35 for learning and lookup. Their actual step, TP range, explicit per-step
  policy overrides and Set identity remain unchanged.
- Replay's copied-tape detection omitted the symbol and entry time. Distinct
  symbol/config observations could disappear when exits and returns happened to
  coincide. Native and replay now use the same exact exit, symbol, gross result,
  entry and direction identity. Actual copies still count once. Equal-time
  replay rows use the native symbol ordering.
- The settings table keyed learned results by step alone. The Short row replaced
  the Long row. The display now preserves direction, shows all configurable steps,
  and maps Wide targets to the shared final Auto table bucket.

Validation: 15 Python step-PF checks and two TypeScript display-model checks pass.
The old methods fail the added repro checks. The corrected native/replay fixture
compares 68 step/direction floors with no differences. TypeScript typecheck passes.
The checks cover causal cutoffs, distinct symbol/entry evidence versus copies,
strict window floors, explicit range/step policies and Wide targets. These are
constructed correctness tests, not a market profitability measurement.

Scope: automatic floor learning remains the existing shared TP-step × direction
model, with a same-direction pooled fallback. Explicit configuration step and
SL-ratio overrides keep their own policy identity and apply consistently through
Base/Main/Real. This change does not introduce independently trained Auto models
for every symbol/indication/type/config, nor promise positive future executions.


## Linked Axis/Block/DCA floors and ratio normalization

The post-Real linked floor now reads the exact positive or negative lane's own
configured Real window. With window-size PF enabled, a config using Real Last-5
and Real Negative Last-50 requests 1.02 and 1.50 respectively; previously native
Axis/Block/DCA used global Last-15 (1.15), and replay used positive Last-5 for both
lanes. Explicit config step/SL floors still apply to both lanes without being
lowered by the global Auto cap.

Decimal midpoint PF ratios now use one half-up normalization in Python and the
desk. For example, 0.125 becomes 0.15 in both, rather than native 0.10 versus desk
0.15. Twenty-eight midpoint cases and extreme finite inputs are covered.
