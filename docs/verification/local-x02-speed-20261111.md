# Local x02, replay tails and test scheduling

The 50-symbol replay still closes 353 parents at execution PF 0.290, versus
selected-window PF 3.063. Every one of 34 substantive report fields, including
all configurations, trades, losses and hourly settlements, equals the frozen
release result. No profitable default is established by this change.

The immutable stage index now feeds native stage verdicts and rank calculations
through read-only indexed sequences. A last-N slice reads only those N results,
without materializing the entire earlier net tape. Native membership, tied-clock
cutoffs, parameter/cost invalidation and expired-context fallback remain intact.
Five alternating repetitions on 248 queries of a 12,000-position synthetic tape
measured a 2.53x median consumer speedup, with identical outputs. This is a
consumer benchmark, not a whole-system guarantee. The full concurrent local
replay took 401.260s; the earlier isolated run took 351.391s. The profiled
reference is not a valid wall-time comparator. Peak replay parent RSS is 1699.5 MiB.

Regression scheduling uses recorded durations as hints, placing long independent
checks first in the least-loaded worker. The ordered memoized replay chain stays
together; every check runs once and result display preserves registry order.
Recorded estimated worker loads change from 228/593/154/218s to
280/351/280/282s. This estimate is not an observed full-suite speedup.

An isolated local x02 runtime uses `ownedQuantityControls: true`: batch,
single and replacement protection carry explicit position quantities, and
market-close fallbacks cannot use aggregate closePosition or positionId.
The old aggregate protection behavior remains the default for other runtimes.
The wrapper's journal independently forbids aggregate closing and account
setting writes. Its empty-account preflight and foreign-position abort remain.
Quantity controls do not automatically grow with stacking; native quantity
coverage checks must certify the new size before further entry admission.

The venue acceptance run passed 22 checks across LONG DOGE and SHORT ADA:
size-bounded SL/TP, actual entries, partial remainders, final closes and zero
stranded test orders. Six actual fills reconcile independently with Decimal
cash arithmetic and the engine within 1e-10 VST. Account fees observed there
are maker 0.02% and taker 0.05% per leg. The separate native 50-symbol startup
also measured every symbol at those rates: its paper cost is 0.110%, including
slippage at 10% of fees. Actual fills already contain slippage and commissions
are deducted once. Acceptance round trips do not establish strategy profit.

Native startup reads account fees in at most four parallel GETs, through the
existing client limiter and the native fee parser. Only the main thread
publishes measured rates. Readiness progress, deadlines and cancellation stay
explicit. The optional full-universe admission mode replays the complete
universe before restoring the live load-governor policy. Large history
pages whose async GET timed out get one bounded synchronous read retry;
successful reads and explicit venue refusals are not resent. Account leverage/margin are not changed by this isolated wrapper;
its conservative 1x margin assumption therefore triggers the native maximum-
leverage self-check as an expected diagnostic, not a verified venue fault.

Live warming exposed page-boundary overlap: counting raw returned candles left
the requested earlier stage context a few minutes short. Backfill now counts
distinct timestamps, requests one boundary allowance within the 1000-row venue
cap, and refuses a page making no backwards progress. A frozen mock reproduces
the defect on the previous implementation and supplies the full contiguous
context after the fix. Short-history and failed-page checks remain required.

A long native warmup also reproduced a replay-clock race: incoming candles
advanced later symbols past the pass decision time, causing future-bar errors.
Each replay now freezes an atomic market view, trims candles after its decision
clock, and uses the same view for peer indications. Incoming history remains
available to the next replay and live entries. Deterministic ingestion-during-
replay and explicit-cutoff oracles preserve the exact reference rows; direct
single-symbol future-bar rejection remains intact.

## Local reproduction

Install locked desk dependencies with `npm ci`, and install Python runtime
requirements with `python3 -m pip install -r requirements.txt` in a local venv.
The default admission policy starts observation when a Set passes the native
Base/Main/Real and own execution gates. It adds no all-symbol sample gate.
Use `--admission full-universe` only for the stricter frozen-universe comparison;
that mode uses complete-universe startup replay before restoring live workload.
Coverage and partially available histories remain reported independently.
The wrapper is an isolated execution option for hosts without systemd/Redis;
production Linux deployment remains under `deploy/`.

Prepare a private mode-0600 JSON credential file with api_key, api_secret,
base_url https://open-api-vst.bingx.com and is_testnet "1". Never commit it.
Prepare a JSON array of exactly 50 explicit symbols. Then run:

```sh
python3 -B scripts/x02_native_session.py --overlay server/pulse/overlay-bingx-x02.json --symbols /private/symbols.json --out /private/new-session --credentials /private/credentials.json --minutes 30 --warmup-minutes 30 --cleanup-seconds 240 --execute
```

Without `--execute` the command only writes its reviewable plan. A
STOP_SESSION file in the session directory requests owned cleanup. The native
session preserves all economic gates and independently records incomplete
warmup, native strategy results and cleanup results. It never fabricates
entries to meet a coverage target.

Run `npm test`, `npm run typecheck`, `npm run build`, and the workflow's offline
ownership/accounting checks. Browser render QA on this host is blocked by
Chromium's denied Unix socket operation; successful compilation is not visual QA.
