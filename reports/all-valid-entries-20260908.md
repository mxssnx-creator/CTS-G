# All valid entry candidates, 2026-09-08

The normal entry dispatcher now retains every eligible indication configuration
(kind, mode, timeframe, direction), alongside general signals. `SetBook.pick_all`
returns every eligible base and trailing Set. A lazy signal × Set matrix uses
shared references instead of allocating millions of order candidates. The
existing bounded, rotating scheduler eventually visits all stable candidates;
ranking no longer discards siblings. A submission error advances to other lanes.

The selected Set is revalidated and passed to `place` without reselection or
cross-pack substitution. New per-config positions include an execution identity
in their control group, pending intents, persisted orderbook and completed trade
records. Identical SL/TP ranges no longer merge distinct execution lanes. Exact
indication lookup prevents a changed signal from being replaced by a sibling.

Pending quantities block only their own lane and reserve both slots and margin.
Block-Active deducts owned/pending adjustment volume only within that execution
lane. Its selected counts remain additive alternatives with the existing cap;
normal execution and adjusted-only switches remain explicit settings.

Compact client IDs handle catalog indices above 999 within 32 characters,
including named installations and isolated control tokens. Legacy IDs and range
groups remain readable. Ambiguous tokens do not identify an arbitrary position.

Partial exits retain a bounded per-position accumulator. The completed result
survives restart and remains usable for own-Set PF after earlier partials have
left the shared 80-row display tape. Empty completed history replays also remove
obsolete symbol evidence, as documented in the server integration report.

Offline evidence includes:

- 250 orders on one symbol with identical risk ranges: 250 separate positions,
  500 quantity-matched SL/TP orders, distinct ownership and restart recovery.
- General plus multiple indication configurations and both directions coexist.
- A 3.4-million-combination view stores 100 signal references and one 34,000-Set
  list, with no materialized Cartesian order queue.
- 100 interleaved partial-close positions, an 80-row shared tape and a restart:
  each Set receives one complete own result with correct quantity and net PnL.
- Own pending/slot limits, stale Set rejection, failed-candidate isolation,
  sibling control preservation, Block-Active volume isolation and large IDs.
- Full engine, unit, release and forced/replay suites are run separately.

These are deterministic offline tests, not actual venue orders or a 12-hour
market replay. Normal transport cooldowns, configured position/Set caps,
qualification, margin, STOP/PAUSE and protection requirements still apply.
Legacy aggregate-control mode remains an aggregate position mode.

The wider request still includes Trend/Break range expansion, 30-step/unlimited-TP
settings, Overall-page synchronization, a full exit-attribution audit and the
requested 12-hour market/configuration matrix. This entry change does not claim
those separate items or unlimited uptime/profitability have been verified.

Publication checkpoint: automatic approval review rejected publishing this new
source to the public `mxssnx-creator/CTS-G` repository. Ownership and push rights
were verified; the remaining requirement is explicit authorization for public
disclosure. Do not bypass that rejection through another transport or connector.
Source commits and protected Git bundles are retained locally.

Final integration checkpoint: GitHub PR #46 independently merged the captured
server work into `dc5f772641a7c803c63f47e54b3d3747075317b0`. The server now has a
clean worktree at `f12253eb908756af0c855f6ae8d0e2496be1a596`; the correctly named
`cts-g-pulse@bingx-x01.service` and `cts-g-pulse@bingx-x02.service` were both
observed active. This work did not install/restart them. This branch incorporates
the newer main and its capacity fields, resolves duplicate declarations/tests,
and retains the entry and history corrections.

The integrated engine suite passed 399/399 checks. Node tests passed (191 script
tests, four optional-file skips, 41 TypeScript tests); typecheck, lint and build
passed. Release and forced contracts passed 12 and 16 tests respectively. Python
discovery passed 123 tests before the final round-robin fairness regression;
the final focused suite passed all 11 entry tests. The dispatcher interleaves
symbols and configurations instead of exhausting one symbol's catalog first.
