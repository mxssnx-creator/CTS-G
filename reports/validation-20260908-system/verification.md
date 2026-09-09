# System, persistence and Redis verification — 2026-09-08

The implementation passes the offline gates below. It has **not** completed the
updated browser or VST installation acceptance tests. The installed trading
application still runs the previous revision; only the separately described
Redis server settings were changed remotely.

## Execution and calculation behavior

- General/historical reference calculations remain enabled regardless of the
  Normal (General) execution switch. Normal defaults to disabled in new profiles
  and applies independently to effective simulated/demo and live execution.
- Block Active has its own execution identity, control group and adjusted
  quantity. Enabling both Normal and Block produces separate orders. Disabling
  Normal does not disable Block, trailing or the reference calculations.
- Block minimum level defaults to 0, the virtual base. Actual Block Active
  entries always represent a positive adjusted increment, at count 1 or higher;
  a larger configured minimum filters eligible Block counts independently.
- Confirmed partial closes use their fill identity. Multiple partial fills of
  one parent count separately; repeated notifications do not count twice.
  Legacy complete-close records retain their client-ID deduplication behavior.
- Existing all-valid scheduling, independent indication ranges, System/Exchange
  overview grouping, Axis visibility, TP/SL ranges and PF/DDT gates are retained.

## Redis and memory boundaries

Redis caches pure historical metric bundles. A cache hit still passes through
the current stage, direction, strategy and execution qualification. Calculation
inputs are captured before network access; signatures and checksums reject stale
or corrupted bundles. Live-only calculation inputs never use a historical-only
bundle. A failed lookup computes locally and applies a bounded retry interval.

| Boundary | Default behavior |
|---|---|
| Native Redis connections | At most 4 per client pool; short connection/read timeouts |
| Calculation pipeline | At most 32 Sets per batch |
| Cached Sets per connection | Maximum 350; at capacity retain the latest 280 |
| Calculation versions per Set | Maximum 350; at capacity retain the latest 280 |
| Per-Set byte budget | 256 KiB, reduced toward 80% when reached |
| Total calculation cache budget | 64 MiB per connection; conservative payload/overhead accounting |
| Cache lifetime | 6 hours; independent of durable statistics |
| Low-reuse threshold | Below 5% reuse over 350 observations, calculate locally and recheck after 300 seconds |
| Configuration cache | At most four known hashes per process; 1-second cache |
| Configuration writes | Maximum 512 fields, 256 KiB total and 16 KiB per value by default |

Retention is ordered by source timestamp, including out-of-order arrivals.
Equal timestamps retain insertion order; duplicates do not move an older entry.
Global cleanup processes at most 128 old Sets per Redis script and continues
toward its 80% target when necessary. Cache removal never removes a Set from
the full calculation catalog or changes a position, pending fill or credential.
The low-reuse threshold prevents full-catalog scans from repeatedly filling and
evicting the small cache or needlessly growing Redis's append-only file.

The complete reference catalog still contains **43,680 Sets**. The 350 ceiling
is a database/cache boundary, not a catalog-selection or order-count ceiling.
Existing shorter engine evaluation tapes remain bounded; this change does not
inflate them to 350 rows merely because the cache maximum is 350.

## Durable statistics and System settings

Each connection has its own `statistics/<connection>/statistics.sqlite3` under
the persistent data directory. Code replacement does not replace this directory.
The database retains cumulative confirmed financial totals independently of
recent detail retention and uses WAL, bounded rows, a main-file page ceiling,
verified rotating backups and explicit connection-scoped reset operations.

Reset backs up a consistent snapshot while holding the database write boundary.
Telemetry reset and financial-statistics reset are distinct. Neither modifies
open orders, pending fills, credentials, settings or evaluation histories.
Confirmed fill replays are deduplicated; retained-journal reimport does not
restore pre-reset totals. This is bounded-journal recovery, not a substitute for
an arbitrary-age exchange audit after a prolonged loss of local evidence.

System settings expose worker/candidate/time budgets, memory ceilings, REST
rates, report/sampling intervals, history retention, database row/byte limits,
logs, backups and Redis thresholds. Overview footers expose process CPU/RSS,
database rows/bytes, request rate, recoveries, unclean restarts, session duration
and shared Redis metadata. CPU is per process, with one core represented as 100%.
Stale measurements are marked unavailable rather than shown as current zeros.

## Completed verification

| Gate | Result |
|---|---|
| Python discovery | 185 tests passed, including SQLite and Redis regressions |
| Complete engine suite | 399/399 checks passed |
| Release/import/namespace contracts | 13 tests passed |
| Forced configuration contracts | 16 tests passed |
| JavaScript tests | 191 passed; 4 pre-existing absent-skill checks skipped |
| TypeScript tests | 49 passed |
| Type checking, ESLint, production build | Passed |
| Shell syntax and Git whitespace checks | Passed |
| Parallel order load | 250 Sets produced 500 distinct simulated orders: 250 Normal + 250 Block Active |
| Simulated order quantities | Normal total 12.5 units; Block increments total 3 units; 500 distinct execution/control identities |
| Persistence | Thousands of trades, bounded detail files, verified backup rotation, actual process crash/restart, scoped reset and concurrent readers/writers |

Redis tests execute the production Lua scripts in fakeredis with its Lua
interpreter. They cover chronological 350→280 pruning, per-Set/global byte
pressure, lane isolation, config-write bounds, cache/uncached PF-DDT equality,
all indication identities and Normal/trailing packs, changed settings, corrupt
cache input, outages, low-reuse protection and concurrent input changes.
fakeredis does not implement `MEMORY USAGE`; only that metadata probe is
substituted in the configuration-hash tests. A native local Redis test server
could not start in this environment, so these are not native VPS benchmarks.

The full market replay reused all 780 confirmed one-minute candles per symbol
(60 warmup + 720 evaluation bars) for XRP, BCH and SOL. Its fixed window is
**2026-09-08 08:07–20:07 UTC**. See `replay-12h.json` and the previously archived
candles in `../validation-20260908/`.

- 43,680 catalog Sets and 131,040 symbol/configuration evaluations.
- 62.89 seconds; isolated replay peak RSS 2,427.9 MiB.
- All eight indication kinds, independent Trend/Break configurations and
  Block/DCA calculation tapes present; progress completed 3/3 symbols.
- PF floor 1.05 admitted 18,118 General SHORT lanes in this data; 1.10 admitted
  13,208; 1.20 and 1.35 admitted none. A stronger gate must not manufacture orders.
- No exchange orders were submitted by the replay or offline load tests.

## Verified remote change

On the existing shared Redis server, these settings were applied without a
restart and persisted with `CONFIG REWRITE`:

| Setting | Before | After |
|---|---|---|
| `lazyfree-lazy-expire` | no | yes |
| `lazyfree-lazy-server-del` | no | yes |
| `lazyfree-lazy-user-del` | no | yes |
| `active-expire-effort` | 1 | 3 |

Owner-only configuration backup:
`/var/backups/cts-g-redis/20260908T225503Z/redis.conf.before`.
Verified SHA-256:
`145eb272ec1d34744a4cf1b350a92e1a269ca628679bd86703df2878d859bd4d`.

Post-change Redis responded to PING. AOF writes and RDB snapshot status were
`ok`; `noeviction` and the 4,966,055,936-byte memory ceiling were preserved.
It still contained 1,490,124 keys and approximately 3.76 GiB of data. The large
sampled indication/position key families are produced by the separate older
CTS-K-N application, not this CTS-G cache. Their configured lifetimes were not
shortened and their data was not deleted. This tuning does not claim an immediate
reduction of that existing dataset.

The installed CTS-G worktree was clean at
`f12253eb908756af0c855f6ae8d0e2496be1a596`. Desk, HTTP and X01 were active with
zero reported restarts; X02/VST was inactive. No trading service was restarted
and no live-money order was placed by this work.

## Remaining acceptance gates

1. Transfer/install this exact source revision on the private VPS, then run
   desktop/mobile browser checks and the VST-only reinstall/restart test with
   the actual demo endpoint verified before any order submission.
2. Measure native Redis/cache latency, memory accounting and sustained operation
   on that installation. Offline tests do not establish unlimited uptime or a
   production performance guarantee.
3. Public push/PR/merge after the publication restriction is resolved.

Prior automatic approval review rejected both the updated private source payload
and public GitHub publication, requiring explicit payload/destination approval.
Those transfers were not retried through another transport. Browser smoke tests
were attempted for dev and built output, but Chromium was absent and its official
download failed. Updated screenshots and browser functionality are unverified.

Relevant implementation references: [Redis pipelining](https://redis.io/docs/latest/develop/using-commands/pipelining/),
[Redis configuration and lazy freeing](https://raw.githubusercontent.com/redis/redis/8.0/redis.conf),
[SQLite WAL](https://www.sqlite.org/wal.html) and
[SQLite size/checkpoint pragmas](https://www.sqlite.org/pragma.html).
