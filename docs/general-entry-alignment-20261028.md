# General Set entry alignment and batch handling

General Set evaluation calls `general_signal` on up to 60 completed one-minute
bars. The entry scheduler previously used a different score that added volume,
outbreak and regime adjustments, and could manufacture general timing from an
indication when the general score was flat. Those choices could send an entry
whose timing was not used by the selected general Set's evaluation. A synthetic
decision mismatch establishes a logic defect; it does not establish the cause
or magnitude of any observed profit-factor change.

The general entry now uses `general_signal` on the most recent 60 completed
stored bars with the same gap handling as Set evaluation. Timestamped exchange
refresh rows are merged into that store; the current forming bar is excluded.
Missing or stale closed history prevents a new general entry, while other
signals and existing position management continue independently. The former
indication-timing fallback for general entries has been removed. The separate
live score remains available for existing coordination and position controls.

Entry batches validate each item before dispatch, retain valid siblings, and
do not switch endpoints after a rate-limit or risk rejection. Only a clearly
identified unsupported batch form falls back to singles. Successful batch rows
without their own client identity are reconciled by client ID rather than
being assigned by position. Existing pending intents remain authoritative.
Recent-window lookups visit the requested minutes rather than sorting the
entire retained symbol history; both paths return identical records.

This is a code-level correction, not proof of sustained profitability or of
actual exchange fills. The full transport, restart, pending-fill, and live
account behavior must be verified on the intended environment before enabling
or changing a running service. No running service or account was changed by
this work.
