# Replay performance and native x02 verification

The economic comparison remains negative. The fixed 50-symbol, 24-hour window
produced 353 closes at PF 0.290, despite selected Base PF 3.063. No losses,
fee debits, qualification thresholds or losing strategy arms were removed.

## Measured performance

| Check | Previous | Updated | Scope |
|---|---:|---:|---|
| Repeated four-symbol processing fixture | 44.12 s | 15.11 s | Same 1,406 Sets and 69,937 rows; four cache hits |
| Two fixture checks together, including cold replay | 104.02 s | 77.53 s | One run per implementation on this host |
| Retained four-symbol memo | Two uncompressed entries; cyclic misses | 7,435,581 bytes | Immutable compressed snapshots; 64 MiB/32-entry caps |
| Full 50-symbol cached replay | Prior run 616 s | 471.418 s | Different concurrent host load; an observed runtime comparison |
| Report payload | Approximately 47.6 MB | Approximately 16.0 MB | All displayed rows/charts retained; complete input JSON retained separately |

The 50-symbol replay checks all shared JSON fields against the previous run,
including all 353 individual trades. Runtime and index provenance are excluded
from equality; next-15 and next-25 cohorts are additional outputs. The index
served 225,584 exact causal queries with no native fallbacks. Six independent
query tests include ties, future-outcome changes, expired context, changed
parameters and an identical native/indexed portfolio.

The regression memo caches raw replay work only. Every check still executes
its own scoring, stage gates and Last-N windows. Snapshots decode into fresh
objects. Pause/geometry/peer/implementation changes invalidate reuse; automatic
pause closures that cannot be serialized run natively. Goldens are unchanged.
The runner balances independent checks by measured duration and identifies
the running checks in its heartbeat. Source-parity tests run before the heavy
four-process suite to avoid starving their computation deadline.

## Concurrent feed correction

Previously, a feed update during a multi-symbol pass could replace a later
symbol's candles and clock after the pass's decision time. That symbol then
failed with a future-bar error while earlier symbols completed. The pass now
uses shallow, coherent snapshots of the symbol/peer candle mappings and their
clocks, and retains the actual replay clock in its provenance. Market arrays
remain shared read-only; live feeds continue updating the live book.

A deterministic regression advances the second symbol while replaying the
first: the previous method reports one error and different tapes; the fix
reports zero errors and exactly matches an independent frozen-book replay.
The live book retains the new candle. All 15 history-snapshot checks pass.

An optional bounded process adapter now performs raw replay outside the
execution process. It uses spawn workers and immutable candle/configuration
frames, at most four processes and a bounded submission window. Workers never
receive an exchange client, positions or qualification state. Native SetBook
still publishes every Set's own rows, scores its own stages and applies all
Last-N rules. Per-worker row-code dictionaries are decoded explicitly; dynamic
pause closures become exact lookups over the same frame. Worker failures fall
back to the original native calculation. Worker shutdown terminates only these
owned calculation children.

Two process-adapter tests compare all tape rows and native stage figures across
four symbols, including fixed pause, automatic pause, Auto Hold, a later frame
and deliberate worker failure. All comparisons pass with zero worker fallbacks
on the successful paths. The adapter remains active for subsequent native
history updates, so calculation work stays outside the order-management loop.

## Native VST session

`scripts/x02_native_session.py` uses a private state directory, 50 explicit
symbols, a unique order tag and a durable mutation journal. It retains native
strategy qualification and native accounting. Quantity-only protection and
closing are restricted to confirmed, exact session-owned fills. Duplicate or
lagging receipts cannot increase capacity. Aggregate closing forms are locally
rejected without dispatch; native quantity fallbacks retain their own coverage
flags. Atomic aggregate replacement is disabled in favor of native
place-then-cancel with exact owned IDs.

Read-only account checks retry unavailable responses three times; failed reads
never prove flatness. Account fees can be resumed from a private account-bound,
one-hour cache. Raw candles may resume from a recent native VST capture; no
qualification scores, positions or simulated fills are restored from it.
Paused startup batches the universe to avoid rescoring the full grid after
every pair. The native critical-memory abort remains enabled, and ordinary
chunking resumes before entries.

Startup attempts opened no orders and ended confirmed flat. The first
encountered a read timeout after collecting all 50 account rates; the second
established that two-symbol rebuilds were the warmup bottleneck. The resumed
bulk session revealed three flat VST histories (DYDX, CHZ, ZRX). They were
replaced by SUI, APT and OP after verifying active contracts, valid full-history
OHLCV and consecutive one-minute timestamps. This selection used data quality,
never PnL. The original 50-symbol replay universe remains unchanged. The healthy
native session is still under observation; its eventual execution results must
be reported separately from the historical replay and manual acceptance.

The test leaves exchange leverage/margin settings unchanged and estimates
margin at 1x. The native max-leverage fixture consequently reports a failure
under that deliberate test constraint; it is not a verified exchange-leverage
test. Native one-position-per-symbol admission also remains in force.

## Local installation and validation limits

Local dependencies were installed with `npm ci`; Python uses an isolated venv
with the SOCKS transport dependency. Typecheck and production build pass.
The local desk starts with `npm run dev`, with a read-only sidecar for the
private native state. The repository startup script resolves its own location.
The shared `/workspace/startup.sh` belongs to another worktree and is preserved.

Complete local regression runs passed 706/706 in 1,307 s and then 707/707 in
909 s, including the new snapshot regression; host load differed. Engine checks
passed 109/109. A full phased run is in progress after a source-parity process
hit its deadline under competing CPU load; the independent source retry passed
5 Python checks and 6 Node checks. Current focused checks: 18 native-wrapper
tests, 4 replay-memo tests, 6 indexed
query tests, and report/script validation. The compact report's script/DOM
contract passes 11 charts, 24 hourly rows, 50 symbols, 97 expanded indication
groups and filter checks. Browser rendering remains unverified: local Chromium
cannot start because the managed environment rejects required socket/ptrace
operations. No browser-render success is claimed.
