# Bounded replay work and native session supervision

The first native attempt became unresponsive after its last verified active
heartbeat at 705.18 seconds. Three SHORT lots still had all six exchange
protections. Independent recovery closed their exact quantities and confirmed
zero positions and zero orders. Six fills reconciled with Decimal accounting;
the combined recovery result was +0.054525 VST after reported commissions.
There were no natural strategy closes. This is a failed observation with a
successful owned recovery, not a completed thirty-minute strategy test.

An additional large replay was killed with exit137 while many completed
symbol results were retained. Replay build now limits submitted/unconsumed
work to the worker budget and compacts each symbol's direction and confidence
columns before retaining the universe. Confidences remain binary64; no result
is rounded or discarded. Checkpoints can use `STAGE_CHECKPOINT_COMPRESS=1`;
cache paths ending in `.gz` use checksum-bound streaming gzip. Existing raw
pickle caches remain readable.

The isolated native wrapper now watches the actual main-cycle heartbeat and
observation deadline from a separate thread. Staleness takes priority over
elapsed wall time, so a stalled loop cannot be reported as a passed run. History
work can stop at a scoring boundary without interrupting main-thread close
accounting. Cleanup runs before diagnostic capture. Periodic capture writes
stats only; the full ready/final candle captures release the shared book lock
before disk serialization. This wrapper does not replace host-level service
supervision against SIGKILL or host failure.

Focused checks compare native versus bounded build results, enforce bounded
completed work, verify checkpoint invalidation/resume and gzip integrity, test
stale/deadline decisions, ensure capture IO releases the trading lock, and
preserve close accounting during history cancellation. Economic qualification
rules and costs are unchanged by these scheduling/supervision changes.
