# Micro qualification and minimum-order sizing — 9 November 2026

The minimum-order audit found a reproducible sizing rejection. For the saved
STRK-USDT contract, the minimum quantity is 53.996 at a price of 0.03735 USDT.
Converting that quantity to notional and back gives 53.995999999999995 in binary
floating point. Rounding down then loses one quantity step and rejects an
otherwise permitted minimum. `Pulse.size_qty` now preserves its already rounded
minimum after validating funding and the optional strict notional budget. The
final margin, strict-budget and sanity checks still apply.

The regression fails on the prior code and passes with the fix. It covers the
ordinary minimum, an exactly funded strict minimum, an insufficient strict
budget and insufficient margin. All 124 trader regressions pass offline. No
account, leverage setting or exchange order is changed. The subsequent owner request lowers the shipped Micro qualification floor to 1.01; this requested setting is not a profitability certification.

## Three-dollar audit

The requested volume factor is 0.05, the existing minimum. With the existing
5% balance sizing rule and a 3 USDT assumed balance, requested entry notional
is 0.0075 USDT. Ordinary sizing has a 0.20 USDT internal floor and can increase
the quantity further to meet venue minimums. The strict budget does not.

The saved public contract quotes and prices for 80 symbols require rounded
minimum notionals between 2.0000358 and 21.28 USDT. Strict sizing therefore
returns zero for all 80. Ordinary sizing returned quantities for 79 before the
rounding fix and 80 afterward; other strategy, portfolio, margin and venue
checks can still reject. This arithmetic uses public maximum leverage only to
test the sizing function. It is not an authenticated account quote, a leverage
recommendation, or proof that the account can fund 80 simultaneous positions.

`scripts/audit_small_account.py` reads saved contract bytes through the native
parser and calls pure sizing methods on an instance created without its
constructor. It never creates an API client or starts a trader. Socket network
access is denied. Balance and size inputs must be finite and positive.

## Bounded Micro experiment

`scripts/micro_feasibility_study.py` freezes 26 development profiles before
scoring: two fee controls, plus core/momentum/reversal sources, 15/60-minute
holds, 0.2%/0.3% Micro targets and two last-N policies. Added sources keep
independent configuration identities and independent/Trend/Break relations.
SL/TP ratios are 0.25–2.50 by 0.25. The existing minimum stop policy adds the
0.1% net distance to modeled costs. Larger targets are not relabeled Micro.

All 80 symbols require 4,320 aligned, contiguous one-minute bars. The first
24 hours warm the engine; the middle 24 hours screen the first 12 symbols
from the preselected public-volume universe; the final 24 hours confirm the
frozen finalist and both fee controls on all 80. The data partly overlaps earlier
research, so this is chronological confirmation, not a pristine prospective
holdout. Public fee quotes must be homogeneous; heterogeneous fees are rejected
instead of averaged. The observed public model is maker 0.02%, taker 0.05%,
with slippage allowance equal to 10% of fees: taker round trip 0.11%. The
conservative configured control uses 0.05%/0.08%, or 0.176% round trip. Public
fee quotes are not account-specific fees or measured fill slippage.

The native strategy lanes, signal calculations, stage gates, rank and Axis
coordinator are used. Twenty sampled gate/rank comparisons per profile must
match the engine. Market bytes, engine source and tape caches are fingerprinted.
Only admission-window variants share identical geometry tapes. Socket access
is denied. The script cannot start an exchange connection or promote a preset.

Research eligibility requires at least ten closes in each development half,
closed PF at least one in both halves, and marked PF at least one. It is
separate from native engine eligibility. When no profile qualifies, a diagnostic
finalist is still confirmed and explicitly remains unpromoted. Every losing,
empty and held cohort stays in the evidence. A high selected historical PF
cannot guarantee high future executed PF.

The simulator retains its native one-parent-per-symbol constraint and does not
model account allocation, Block/DCA added volume, funding or order-book fills.
Unit-return sums and their drawdown are not percentages of the 3 USDT account.
Original source snapshots and results accompany the HTML/evidence report; the
running strategy study was kept on the pre-sizing-fix source so its fingerprints
remain internally consistent. The sizing fix changes no strategy calculations.

Reproduce with saved public inputs, without credentials:

```sh
python3 scripts/audit_small_account.py --market MARKET --contracts PUBLIC_CONTRACTS --out sizing.json
python3 scripts/micro_feasibility_study.py --market MARKET --out study --jobs 4
python3 -B scripts/regression/run.py --only trader. --jobs 2
python3 -B scripts/regression/run.py --only replay-provenance.micro-short --jobs 1
```

The study writes `protocol.json`, every development/confirmation result,
`frozen-finalist.json`, `summary.json` and a continuously updated `status.json`.
Results include hourly validated/evaluated counts, window PF, executable sets,
closed and held returns, half-window results, and individual trade prices.
Current venue PF is unavailable unless authenticated fills are independently
obtained and reconciled. No real-money run is asserted by this work.

## Requested lower positive Micro floor

The later owner request explicitly lowers Micro qualification. The active
`minPfByStep` policy supersedes `minPfMicro`, which describes a window-size
class rather than a TP range. The shipped BingX and Bybit overlays and desk
default now use `minPfByRange: {micro: 1.01}`. The Settings control persists an
explicit range floor, preserves an explicit disable, and shows the effective
override in the per-step table. Other range and Signal floors retain their rules.

A native regression checks all Micro sets from the four shipped overlays at
window sizes 1, 5, 10, 20 and 50, including StageParams, and confirms 1.01 in
every stage. Configuration round-trip tests distinguish this from the unchanged
window-size class. The requested 1.01 and the diagnostic 1.001 floor are also
compared on frozen native tapes in a separately recorded research follow-up.
Neither is represented as proof of positive future executions.

The first hosted suite found a stale deploy pin after the sizing edit
(642 passed, one pin mismatch). The deploy checksum and restore patch were
updated, then all 30 logistics checks passed, including both restore paths.
