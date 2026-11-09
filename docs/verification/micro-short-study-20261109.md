# Micro / Short causal study — 9 November 2026

Engine baseline: `a4ce6ab0e2c2bd3e4ac36994220f265b9b17d5d4` on Stable-02-01.
This change adds a reproducible experiment and two regression checks. It does
not alter an exchange connection, production risk limit, gate or default preset.

`scripts/micro_short_study.py` compares 72 primary configurations on 12 fixed
symbols, with 24 hours prehistory, 24 hours development and a reserved following
24 hours. A separately recorded development follow-up adds 12 Micro-family
configurations. Finalists are frozen across all 84 before confirmation is read.
All candidate outcomes remain in the evidence, including zero-order and losing
configurations. A diagnostic finalist can be ineligible for default promotion.

The matrix includes Micro steps 2–3 and Short steps 7–12; core and independent
AO/common sources; nine Micro signal families plus momentum-change/pullback with
Trend/Break relations; default, 15-minute and 60-minute holds; factory, medium and
fast last-N profiles; negative Main on/off; SL/TP ratios 0.25–2.50 at 0.25 increments.
The requested fast Main window of 5 is clamped to 10 by the native engine. This
effective limit is reported, not silently represented as a 5-order window.

The experiment uses the existing conservative configured fee model: taker 0.08%
per side, maker 0.05%, and slippage equal to 10% of fees (taker round trip 0.176%).
It is not an authenticated account fee quote. The minimum net stop distance is
0.10% above these costs. Micro trailing has no legal cell under the existing
three-cost floor (0.528%), above the Micro target ceiling (0.3%); it is explicitly
reported as unavailable under this policy, not tested successfully.

The production stage evaluator, native signal/geometry tapes, per-entry causal
refresh, Axis eligibility and provenance-verified raw caches are used. Sharing
is limited to admission-only variants with identical signal, geometry, cost and
hold histories. Each profile samples 20 gate/rank comparisons against the engine.
The simulator keeps its native one-parent-per-symbol constraint. The experiment
does not model Block extra volume/legs, DCA, funding, venue latency/order-book
fills or account allocation. It cannot establish complete live equivalence.

Selection requires at least ten closes in each development half, PF at least
one in both halves and PF at least one when held positions are marked. Eligible
profiles rank by their weaker half, then net return and order count. This is a
research selection criterion, not a new minimum-order gate in the engine. Empty,
flat, unknown and no-loss cohorts remain distinct. All subsequently admitted
losses remain in execution figures, even when their configurations stop passing.

Reproduction:

```sh
python3 scripts/micro_short_study.py --data MARKET --base OVERLAY --out study --jobs 3
python3 scripts/micro_short_study.py --micro-families --data MARKET --base OVERLAY --out followup --jobs 3
```

Copy the 72 primary cases and only the 12 additional `microfamily` cases, plus
their trusted cache files and metadata, into a fresh combined directory. Retain
the original two protocols. Then run:

```sh
python3 scripts/micro_short_study.py --include-micro-families --finalists --data MARKET --base OVERLAY --out combined --jobs 3
```

Validation: 642 engine regressions passed (592 seconds, three workers), 109
engine-control checks passed, 191 Node tests passed with four declared skips,
104 TypeScript tests passed, and typecheck/build passed. An initial TypeScript
test invocation lacked this checkout's dependencies; the affected suite passed
after restoring the existing lockfile-matching dependency directory. No browser
or live venue execution is asserted by these checks.

The full per-profile, hourly, symbol, source, type, lane, configuration and stage
window tables are in the accompanying HTML/evidence report. Historical passing
window PF and subsequent modeled settled/marked execution PF are distinct. Unit
return sums and their drawdown are not account-equity percentages. Future high
PF is not guaranteed by historical qualification or a selected replay window.
