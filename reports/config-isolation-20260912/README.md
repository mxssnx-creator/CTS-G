# Configuration evidence regression audit

This focused seven-day endpoint audit is separate from the causal admission
sweep in `../window-sweep-20260912/cts-g-seven-days.html`. It diagnoses the
Base/Main/Real calculation contract, not executed account returns.

- BCH/SOL/XRP; 96 risk Sets per symbol, both directions and Last-N 5–75/5.
- 189,449 simulated closes; 8,640 Set/window/direction evaluations.
- 122 Base-qualified, 82 Real-qualified endpoint evaluations.
- 2,115 cooperative progress callbacks; 20.656 seconds.
- No network order requests and no live-order-count claim.

`config-evidence.html` and `evidence.json` preserve the detailed parameters
and stage-specific samples. The browser settings contract verifies Base
Last-N and one overall PF threshold reaching all stage aliases. Optional
external font/branding requests were stubbed only in the local browser harness;
product code, authentication and branding were preserved.

Final local checks: engine 410/410; Python full suite 358 passing before the
final indication-gate regression, then 11 isolation tests, 24 order-entry tests
and 7 connection-profile tests passing after it. CI runs the complete final
suite. Release 15/15; forced grid 16/16; JS/TS 261 pass, 4 deliberate skips;
typecheck and production build pass. Desktop/mobile dev and production
browser checks pass. The 700-Set rate-limit regression opens every fake order
exactly once, retains the remaining queue, and verifies 1,400 protective orders.
This is explicitly an offline fake-exchange test.
