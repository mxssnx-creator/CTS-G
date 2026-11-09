# Block and post-Base integrity — 9 November 2026

The user's priority is positive post-Base performance through execution. These
repairs correct qualification and accounting; the measured executions below
remain losing. No connection was started and no exchange orders were submitted.

## Repairs

- Block v2 uses the parent's explicit range/step PF floor. Micro 1.01 is no longer
  silently replaced by the legacy 1.02 minimum. Volume-dependent uplift remains;
  legacy BlockBook retains its existing default floor.
- A partially filled count completes its original target. Its full target is not
  counted twice in the PF threshold for the remainder, for Shared or Additive.
- Recovery accounting weights add returns by entry notional, including the
  changed entry price. Complete actual base/add entry and exit fees replace
  modeled costs once. A concrete modeled loss of -0.000448 per base notional
  previously read as +0.00033077 and could falsely reset recovery.
- Unscored or invalid-price closes retire their parent without changing evidence,
  recovery net, carried level or pause. Their audit records remain available.
- Block/DCA replay accepts each qualified parent lane when both Positive and
  Negative pass; the preferred lane for a new entry cannot revoke an existing
  parent's independent lane. Block replay uses the same notional weighting.
- Fixed-target Block adds require positive remaining target room after modeled
  costs. The guard is wired through the live engine and replay. Exit/trailing
  lanes that do not honor fixed TP are exempt. This is a necessary feasibility
  bound, not a guarantee that a target will be reached or fills will match it.

No strategy sizes or active account settings were changed. Existing cumulative
Block ledgers are preserved, not reset; their old recovery totals cannot be
retrospectively reconstructed from aggregate state alone. Carry remains disabled
in the shipped defaults. Recovery sums normalized parent returns, not wallet
cash across parents with different original notionals.

## Verification

New regressions first reproduced linked-floor/partial-fill failures, false
recovery and fee weighting, and unscored-close lifecycle updates. The final
focused Block suite passes 16 checks; trader passes 124; logistics passes 30,
including both restoration paths for trader blob
`30f19d06e5b6c89aaf61310c60787a076f87451f`.
The replay fixture checks dual-lane eligibility and an explicit add cash oracle.
Full hosted checks are required on the final PR head before merge; the report
records their actual run and conclusion.

## Historical sensitivity

`scripts/block_postbase_study.py` uses SetBlockBook itself with frozen Micro
parents from the preceding study: 12 symbols on the development day and 80 on
the following day, with 24 hours of preceding history. The matrix is 26 cases
per day: off, shipped, and 24 Shared/Additive combinations of steps 0.5/1,
total quantity budgets 0.2/0.6 and Set, Symbol+Overall(step), or all four relations.
Shared and Additive pairs intentionally use equal total exposure for parity;
they are not independent statistical samples. All 52 cases are reported.

The script observes completed minute closes strictly before the parent's exit
bar. Same-clock decisions precede closes, so a new result cannot inform those
decisions. Each add requires its parent's own current stages, Axis and recorded
parent-close gate. All losses remain. Held positions are marked separately and
never force-closed into qualification evidence. This does not model account
allocation, exchange minimum lots, partial-fill latency, funding or order books.
It is an adaptive historical diagnostic, not a pristine prospective holdout.

| Confirmation case | Settled parents | Settled adds | Parent+Block PF |
| --- | ---: | ---: | ---: |
| Block off | 1,449 | 0 | 0.842094 |
| Shipped Block | 1,449 | 4 | 0.828311 |
| All 26 cases | 1,449 each | varies | 0.828311–0.842094 |

All confirmation cases retain 38 held parents. No case achieves PF >=1 in both
halves with at least ten closes per half. Development PF ranges 0.296241–0.298346
over 61 parents and eight held. Therefore no Block preset is promoted as
profitable. The target-room guard reduces confirmation added fills across the
matrix from 955 to 887, including held adds. These totals include repeated
sensitivity arms, not independent real exchange orders.

The native lifecycle is checked against independently summed cash equations
70,876 times across the final matrix. The preceding Micro study independently
checked 13,645 closed returns, 212 held marks and 780 native stage/rank cases.
Selected-window post-Base PF and later settled PF describe different trades and
time periods. A high first number cannot be copied into execution statistics.
No claim of permanently high or guaranteed future PF is supported.
