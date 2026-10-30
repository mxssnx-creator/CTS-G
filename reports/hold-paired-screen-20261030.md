# Paired trailing-hold screen — 30 October 2026

This is an offline signal-level counterfactual, not Base/Main/Real validation, a portfolio backtest, or exchange execution. Eight previously used BingX one-minute datasets cover 29 October 03:07–15:06 UTC prehistory and 29 October 15:07–30 October 03:06 UTC forward. The forward sample is not an untouched holdout. Input hashes and exact results are in the adjacent JSON.

For each eligible sampled entry, the same Set, price, direction and subsequent candles are used for a 30-minute hold and a no-time-stop trailing hold. Observations run for 180 minutes; a position still open is marked to that final close with one estimated 0.12% round-trip cost. The timed policy is a candidate, not the configured global reference (360 minutes in this overlay). Entries are sampled every 15 minutes and can overlap: these are equal-notional counterfactuals, not a tradable portfolio. Cohorts crossing the pre/forward boundary or without a full horizon are omitted. Stops/trails use the code's same-bar OHLC ordering, which cannot establish intrabar price order, fills, or slippage.

The prehistory decision requires at least 10 matched observations, a finite unlimited PF ≥ 1.2 and at least 2% above the timed PF. None qualified. Even without the PF floor, the two General candidates that looked better on prehistory performed worse forward. These negative raw-pool figures do not measure selected post-Base or executed PF; those and the post-Base validated percentage are unknown.

| Set | Pre n | Pre PF 30m / no limit | No-limit marked open | Forward n | Forward PF 30m / no limit | No-limit marked open |
|---|---:|---:|---:|---:|---:|---:|
| Indications trail 0.36 / 0.36 | 202 | 0.2516 / 0.2662 | 39 | 258 | 0.4118 / 0.4451 | 1 |
| Indications trail 0.72 / 0.36 | 202 | 0.2487 / 0.2699 | 39 | 258 | 0.5998 / 0.6564 | 1 |
| General trail 0.36 / 0.36 | 31 | 0.1379 / 0.7732 | 2 | 46 | 0.3915 / 0.3242 | 0 |
| General trail 0.72 / 0.36 | 31 | 0.1652 / 0.8385 | 2 | 46 | 0.4441 / 0.4046 | 0 |
| Short Range trail 0.36 / 0.36 | 10 | 0.2750 / 0.2908 | 0 | 30 | 0.6294 / 0.6294 | 0 |
| Short Range trail 0.72 / 0.36 | 10 | 0.2750 / 0.2908 | 0 | 30 | 0.8037 / 0.8037 | 0 |

The engine now refuses to auto-select unlimited holds from completed-only evidence; existing positions retain their stored hold policy. Timed candidates also require enough reference closes. A separate reproducible fixture showed that joining independently replayed shadow lanes can create overlapping positions for one Set/symbol. On such a collision or cooldown violation the engine uses the reference tape and reference hold, and exposes `holdAutoBlocked` in the Set snapshot.

This fallback prevents the demonstrated double-position tape, but does not prove that a non-overlapping splice reproduces all entries of a single dynamic state machine: an unselected shadow position can suppress an entry that would otherwise occur. Candidate histories are also capped, so older entry-time decisions can lack evidence. Rebuild and validate a single dynamic hold lane against a per-bar walk before claiming complete hold-auto accuracy. The older `scripts/hold_auto_eval.py` uses a current-choice historical tape approximation; do not use it as proof of causal engine parity.

Reproduce the paired screen with `python3 scripts/hold_paired_screen.py --data DATA_DIRECTORY --out RESULT.json --jobs 4` using the exact hashed inputs. No exchange calls are made.

## Reduced-grid stage snapshots

`hold-guard-8symbol-snapshots-20261030.json` records three offline snapshots after this guard, on the same eight symbols with 12-hour rolling histories, a reduced 133-Set grid, one SL ratio, four trailing variants, and the audit script's negative minimal-window mode. These override the full live grid and are not an independent 12-hour execution backtest. Cost is 0.12% once. Counts are Set-window records, which may share market entries.

| Snapshot UTC | Base-passing Sets / all | Base paper PF / records | Main-passing Sets | Main paper PF / records | Eligible after all gates |
|---|---:|---:|---:|---:|---:|
| 29 Oct 15:07 | 8 / 133 (6.0%) | 1.9611 / 400 | 0 | — / 0 | 0 |
| 29 Oct 21:07 | 41 / 133 (30.8%) | 2.7713 / 2,050 | 15 | 3.9818 / 86 | 0 |
| 30 Oct 03:07 | 15 / 133 (11.3%) | 2.2151 / 750 | 9 | no losses / 37 | 0 |

The final Main value is reported as “no losses” because the displayed 99 is a sentinel, not a measured finite PF. The script reports two Sets passing validation at the final snapshot, but neither passed all eligibility gates. No simulated executions or exchange fills were observed by this snapshot audit. Exact PF and validation counts from this reduced grid should not be generalized to the full system.
