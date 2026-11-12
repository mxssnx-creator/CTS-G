# Native deadline and terminal evidence

The native x02 observation stopped at its main-cycle heartbeat deadline while a synchronous entry request was in flight. Keep that watchdog and its failed observation result. Slow entry placements now yield between requests at the existing scan-time budget; remaining eligible candidates progress on later cycles, with no additional position or qualification cap.

Cleanup formerly queried every intent separately, omitted batch acknowledgment IDs and could spend its whole budget on unavailable single-order history. It now binds exact batch/client/venue identities, reads bounded per-symbol archived order histories with four read workers, checks symbol, side, position side, type and status, and retains missing/ambiguous/unknown writes as unresolved. The deadline prevents starting further closes or audit reads after expiry. An in-flight synchronous request remains subject to the transport timeout; no interrupted write is resent.

Focused regressions cover exact large IDs, archived market/batch controls, foreign or ambiguous evidence, expired deadlines and continued progress of every eligible entry across slow cycles. No profitability threshold or economic default is changed. A flat venue snapshot is separate from proof that every unknown write reached a terminal state.
