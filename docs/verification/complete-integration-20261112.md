# Pending branch integration and exact fill commissions

The integration retains the histories and newer implementations of #163,
#166, #168 and merged #169. The older #163 merge contributes no net source
change after the newer strategy/volume implementation. Native replay workers
retain the thread and synchronous main-cycle watchdog, bounded snapshots,
history cancellation and cleanup-before-diagnostics from #169. Independent
Last-N blocks, signed short/minimal policies, volume settings and owned
trailing pacing remain available. No economic qualification floor is lowered.

BingX order acknowledgements omit commission fields. Entry and control paths
therefore retain explicit rate-estimate provenance without extra network IO.
At final settlement, persisted cumulative order scopes can replace those
estimates with exact commissions. Symbol, direction, side, order/trigger ID,
filled quantity, weighted price and currency must all agree. Every component
must be complete before fee totals are promoted atomically. Actual fees
replace the existing totals; they are never an additional cost deduction.
Missing or failed reads retain estimate provenance. Partial/repeated control
polls replace the same cumulative component, and the components survive a
restart. Exact zero fees and signed rebates remain valid.

Focused checks pass: 31 native wrapper checks, seven fee reconciliation
checks, 19 settings checks and three disjoint-window checks. An authenticated
read-only comparison of six prior owned VST fills matches independently
reported rounded commissions exactly. This verifies the fee reader, not a
new strategy observation. The source restore pin is regenerated. Complete
combined-head hosted checks remain required before merge.

The frozen 50-symbol, 97-pack, 4,272-Set replay at 0.110% modeled round-trip
cost completed with 60/60 native gate/rank matches. It retains 374 closes,
selected Base-window PF 3.164, execution PF 0.299 and 99.391% full Base-N
coverage. These later losses remain in the report. Contemporary account fees
are assumed for historical modeling. This replay used the separately pinned
#169 calculation source, before this additional integration. It is not
profitability evidence for this combined source. No profitable preset is
promoted and no x01/mainnet deployment is included.
