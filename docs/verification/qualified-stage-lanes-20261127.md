# Qualified stage lanes and simulated execution

## Execution and lineage

Each qualifying Base, Main, Real and Exec window has an address under its exact parent. Identical histories share computation. A child receives a position only if its parent qualified when that position entered. Window predicates use completed observations strictly before the signal. Active positions keep their entry identities through settlement and restart.

A funded entry requires an actual Base entry on the same market and completed minute, a current Exec decision for that set and the matching side. When several independent paths qualify, a deterministic choice allocates the finite symbol/side slot. Mature verified settlements update the gate for their own symbol, side, root set and full path. One unfinished Base position can admit its own entry and prevents repeated signals from starting another position in the same Base lane. A validation-only configuration change withdraws approvals and reuses signal and trade tapes. Corrected dated candles replace their own timestamps.

## Archived candle replays

The research preset uses two 1-minute indications, step 8 and SL/TP ratio 1. Both directions have independent root sets. The profile was selected from the November 21–23 exploration, where RSI and confluence had repeat admissions with this geometry. The same four roots and the same stage settings were then used on every reported market. UTC windows include their start and exclude their end. The simulator deducts the configured 0.11% round-trip cost once per position. PF divides total positive settled net returns by the magnitude of total negative settled returns. Open positions are counted separately. These runs model fills on candles.

| Trading dates in 2026 | Purpose | Symbols | Prehistory hours | Settled | Open at end | Exec PF | Market manifest digest |
|---|---|---:|---:|---:|---:|---:|---|
| Nov 14–Nov 16 | Reserved retrospective window | 30 | 48 | 304 | 0 | 1.543 | 920ffa095c32 |
| Nov 17–Nov 19 | Earlier small universe | 12 | 24 | 25 | 0 | 0.427 | 3c32fd248bd7 |
| Nov 21–Nov 23 | Profile selection window | 32 | 24 | 217 | 0 | 1.643 | ea679b7716f1 |
| Nov 24–Nov 26 | Subsequent window | 32 | 24 | 188 | 5 | 1.370 | 239678028d5a |

The broader 48-set profile settled 406 orders at PF 1.227 on November 21–23 and 404 at PF 0.901 on November 24–26. Its 12-symbol runs settled 41 at PF 0.578 on November 17–19 and 49 at PF 1.231 on November 24–26. The narrow profile improved both large-universe comparisons while reducing the number of orders. Both profiles lost on the earlier 12-symbol sample. The preset is opt-in.

## Post-Base evidence

Each row is the final hourly Base diagnostic of the narrow profile. The numerator counts passing signed Base window evaluations across the four roots. The PF uses subsequent admitted paper positions with duplicate membership removed within each root. Each Exec figure above covers the entire trading period.

| Snapshot in UTC | Passing / evaluated | Passing | Post-Base positions | Post-Base PF |
|---|---:|---:|---:|---:|
| Nov 15 23:00 | 1 / 32 | 3.12% | 5 | 0.190 |
| Nov 18 23:00 | 4 / 32 | 12.50% | 58 | 1.241 |
| Nov 22 23:00 | 2 / 32 | 6.25% | 56 | 1.081 |
| Nov 25 23:00 | 2 / 32 | 6.25% | 173 | 1.478 |

## Correctness and resource checks

The independent scalar walk enumerates every qualifying identity at every depth on a signed mixed-return fixture and checks exact parent membership and sampled order streams. Additional cases cover same-clock settlements, out-of-order and repeated receipts, stage-only reloads, native Base entry timing, candle corrections, expiry and the finite venue slot. A 320-position all-positive fixture with the complete default grid addressed 25,600,000 qualifying Exec identities out of 6,553,600,000 possible identities per root without allocating an order ledger per potential path.

On the 1,800-row benchmark, the full-history reference and the bounded scorer produced the same 1,216 qualifying Exec paths and the same order evidence. Median evaluation time over five trials was 261 ms and 106 ms respectively. Peak Python allocations were 4,220,859 and 2,869,808 bytes. These measurements cover the scorer on this fixture. Per-symbol compressed shadow data across each broad 32-symbol run used less than 1 MiB in total per replay cache.

The benchmark and regression tests can be run from the repository root.

```sh
python scripts/test_stage_grid.py
python scripts/benchmark_stage_grid.py --rows 1800 --trials 5
npm test
```

Run this command against the archived minute-candle directory for a reported window. Choose the matching symbol count and a different cache path for each dataset. The manifest digest above is the first twelve hexadecimal digits of SHA-256 over the sorted, compact JSON mapping from symbol to market file SHA-256.

```sh
python scripts/trade_sim.py --data "$CANDLE_DIR" --symbols "$SYMBOL_COUNT" --last-hours 48 \
  --patch presets/reversal-confluence-rsi-stage-study.json --refresh 0 --eval-every 3600 \
  --stage-details --jobs 2 --cache "$REPLAY_CACHE" --out "$REPORT_PATH"
```
