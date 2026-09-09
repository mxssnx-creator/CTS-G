# CTS-G verification — 8 September 2026

The server changes are integrated with main, and the additional fixes are prepared
on `codex/all-valid-entry-sets-20260908`. Deployment of the latest changes remains
blocked by automatic approval review. No trading service was restarted by this
work, and no real-money exchange order was submitted.

## Changes

- Every valid Set and indication configuration reaches a fair, bounded scheduling
  queue. Independent execution lanes retain their own order identity, quantities,
  partial fills and completed-trade accounting. Protection reconciles full
  symbol/direction groups while preserving each configuration's quantity.
- Desk, Results and Settings share four selection levels: System/Exchange,
  indication, TP range, and Normal/Trailing/Axis/Block/DCA. PF, DDT and sample
  counts use the selected source. Axis statistics disappear when disabled.
- Trend defaults to three independent EMA calculations (5/13, 8/21, 13/34), and
  Break to 8/16/32-candle ranges. Settings expose the ranges; historical records
  retain configuration identity. Signal changes invalidate obsolete history.
- Overall Settings exposes synchronized step, PF, DDT, control-order and symbol
  controls. TP defaults to 0.3% minimum and unlimited maximum (stored as zero),
  SL to 0.15–3%, maximum step to 30, and PF thresholds to 1.05–1.35.
- Pending counts reflect the actual order queue; event outcomes avoid counting
  one rejection twice. Historic progress counts the current valid universe.
  Gzip WebSocket Ping receives the required text Pong, and failed sessions close.
- Block-Active subtracts only its own executed/pending volume. Its reference
  expiry sweep is shared across candidates, avoiding a full scan per candidate.
  Per-reference expiry remains exact. A VST-only launch guard rejects a real
  endpoint before creating the exchange client.
- Mobile card sizing and built-preview endpoint forwarding were corrected.

## Automated evidence

| Check | Result |
| --- | --- |
| Python discovery | 147 tests passed |
| Engine checks | 399/399 passed |
| Node script tests | 191 passed; four optional-skill checks skipped |
| TypeScript tests | 47 passed |
| Typecheck, lint, production build | Passed; database migration skipped with DATABASE_URL unset |
| Independent-order simulation | 250 distinct orders on one symbol, unique IDs, restart restoration |
| Partial execution accounting | Interleaved partial closes preserve quantities and round-trip PF |
| Filter combinations | 240 exact combinations plus parent-selection resets |
| Block reference expiry | 1,000 candidates share one sweep per interval; stale references cannot enter |

## Exact twelve-hour market replay

The evaluation window is **8 September 2026, 08:07–20:07 UTC**, with 60 additional
warmup candles per symbol. XRP-USDT, BCH-USDT and SOL-USDT each have 780 validated,
consecutive exchange candles. Public candle reads used the existing server
fetcher; replay ran separately without credentials or trading calls. Compressed
input candles and their hashes are included alongside `replay-12h.json`.

The full catalog has **43,680 Sets**: two packs, 30 SL:TP ratios, steps 3–30,
Normal and all 25 trailing pairs. Three symbols give **131,040 symbol/Set
evaluations**. Replay and scoring took 65.09 seconds, with peak RSS 2,427.3 MiB.
Progress completed at 3/3 symbols. The bounded overview is 160,684 bytes across
125 filter groups. All eight indication types, the six Trend/Break configurations,
Block, DCA and Block/Signals produced separate historical evidence.

| PF floor | Qualified General SHORT | Qualified Indications SHORT | Qualified LONG |
| --- | ---: | ---: | ---: |
| 1.05 | 18,651 | 1,254 | 0 |
| 1.10 | 13,858 | 0 | 0 |
| 1.20 | 4 | 0 | 0 |
| 1.35 | 0 | 0 | 0 |

Every selected candidate passed the exact execution qualification check; raising
the PF floor never enlarged the qualified set. Calculation-enabled counts are
reported separately from entry qualification. These results explain why a high
configured PF floor can legitimately produce few orders. They do not establish
exchange profitability or authorize a live profile change.

The retained per-Set tapes contain 3,494,400 simulated samples: 2,151,183 Scratch,
435,763 SL, 64,328 TP and 843,126 time exits. These are independent configuration
observations, not summed account trades. This market window did not demonstrate
every possible exit type; targeted engine tests cover the remaining conditions.

## Browser and remote status

The isolated server copy based on `70ba32e` plus the first mobile correction
rendered on desktop and mobile with no console errors or horizontal overflow on
the Desk. Manual Exchange → Trend → 0.3% → Block selection showed the matching
source-specific rows. Disabling Axis removed both its tabs and rows. The Overall
Settings fields were observed in the browser. Settings' Set page exposed all four
filter levels, hid Axis when disabled, and had no mobile horizontal overflow.

Browser QA also found missing data-endpoint forwarding in the built preview and
mobile overflow in Results. Both have source fixes, but final browser verification
of those fixes and the new range inputs is **pending** because the updated
source transfer was rejected. Read-only fixtures do not verify saving production
settings or real exchange processing.

The documented project server was reverified through pinned Chisel/SSH:
`152.53.114.112`, hostname `v2202607384858486523`, installed CTS-G source
`f12253eb908756af0c855f6ae8d0e2496be1a596`. That commit is an ancestor of the
integrated branch. Redis confirms X02 uses `https://open-api-vst.bingx.com`.
The last read showed the existing demo engine running, zero open internal and
exchange positions, and one actual pending-order record. A high exchange order
count has **not** been established.

## Release blockers and concrete next actions

Automatic approval review rejected the latest private source archive to the
verified server backup/QA paths, requiring explicit approval for the payload and
destination. It separately rejected publishing this work to the public repository
`mxssnx-creator/CTS-G`. No alternate transfer or publication path was used to
bypass either rejection.

After explicit approval: transfer the committed source to the documented private
CTS-G server paths, verify its checksum, finish desktop/mobile production QA,
back up the current X02 state and unit, install an isolated release with the
VST-only guard, restart X02, and measure exchange fills, control quantities,
partial reconciliation, events and overview consistency during a demo soak.
The real-money lane must not be started or restarted for this validation.

The release is not yet declared production-ready. Unlimited uninterrupted uptime
and 50–250 exchange orders require deployment evidence that these offline tests
and the unchanged server cannot provide.
