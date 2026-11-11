# Bounded compact regression memo

The repeated full-grid test fixture spans four symbols. A two-symbol LRU evicts
the previous pass before any entry can be reused. The harness now retains up to
four symbols and two million observations, stored in compact native columns.
An oversized symbol is replayed normally without retaining it. This changes
test execution only; the trading engine does not import this memo.

Every hit reconstructs fresh row dictionaries with the original Set identity.
Verify mode also reruns the native replay and compares every row and signal
input. Cost, candle, distance, global minimum, topology and clock inputs remain
part of the identity. Peer-dependent imported signals and pause-dependent
replays use the native path rather than a symbol-only memo.

Five focused checks cover exact four-symbol native parity, input invalidation,
copy isolation, row-budget eviction, oversized tapes and the dependent-input
fallbacks. CI runs these alongside the scheduling and complete engine suites.
The optional controlled benchmark alternates two/four/four/two-symbol limits
on identical seeded histories; it measures repeated fixtures, not whole-suite
wall time or trading-loop latency.
