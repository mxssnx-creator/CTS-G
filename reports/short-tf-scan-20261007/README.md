# Short-timeframe edge scan (1 / 5 / 15 / 30 min), 2026-10-07

**Question:** Does any signal family make money on 1m, 5m, 15m or 30m bars, with trades held 5 to 30 minutes, after the desk's 0.18% round-trip cost?

## Method
- **Data:** BingX 1m candles, 60 days, 2026-07-24 → 2026-09-21 (out-of-sample test days from 2026-08-07), for the 12 desk symbols.
- **Grid:**
  - timeframes 1, 5, 15 and 30 min;
  - TP 0.3 / 0.5 / 0.8 / 1.2 / 2.0 %;
  - SL:TP 0.6 / 1.0 / 1.5 / 2.0;
  - hold 5 / 15 / 30 min.
- **Families:** 11, with 480–3,060 configurations each.
- **Walk-forward:**
  - Each test day trades the top 3 configurations of a family, picked on the 14 days before it.
  - 46 out-of-sample days.
  - Entry is the next 1m open after the signal. SL is checked before TP, and each trade pays the 0.18% cost.
- **Survival rule (fixed in advance):**
  - PF ≥ 1.05;
  - ≥ 60% of days positive;
  - every third of the period positive;
  - ≥ 30 trades per day;
  - ≥ 55% of hours positive;
  - positive with cross-symbol clusters counted once;
  - positive with any one symbol removed.

## Result: no family passes. All are below PF 1.0 out of sample.

| Family | OOS trades | Trades/day | PF | Net Σ % | Days + (of 46) | Hours + % |
|---|---:|---:|---:|---:|---:|---:|
| reversion-confirm | 368 | 8.0 | 0.96 | −29.3 | 9 | 41 |
| reversion | 664 | 14.4 | 0.91 | −110.3 | 10 | 39 |
| momentum | 653 | 14.2 | 0.89 | −124.2 | 5 | 29 |
| volume-fade | 50 | 1.1 | 0.89 | −9.8 | 1 | 25 |
| volume-follow | 312 | 6.8 | 0.87 | −72.7 | 6 | 21 |
| reversion-btc-calm | 151 | 3.3 | 0.84 | −43.8 | 1 | 32 |
| reversion-with-trend | 75 | 1.6 | 0.82 | −24.0 | 1 | 36 |
| breakout, fade-breakout, squeeze-breakout, trend-pullback | 0 | 0 | – | 0 | 0 | – |

The last four families never cleared the training gate on any day, so they never traded out of sample. Not even their in-sample results were positive.

This matches the two earlier scans (`reports/edge-scan-20261007`, 13–60 days, holds of 1–24h). On 5–30 min holds, whatever gross edge these families have is smaller than the 0.18% round-trip cost.

## What ships
- **No new short-timeframe kinds.** Adding them would trade a measured loss. 30m bars were not added to the engine, because nothing on 30m passed.
- **1m lanes are now short trades:**
  - every non-1h lot closes at `shortMaxHoldS` (default 30 min, set on the desk);
  - the Set replay time stop (`setHistTimeBars`, default 30) is capped to the same minutes, so the replay judges the same trade the desk places.
- **Live evidence still decides:**
  - The existing 1m indications keep trading only where the Set gates and the desk live edge guard allow.
  - When the last 50 live round trips of a direction have cost-PF < 1.00, entries drop to probe minimum lots with no Block/DCA adds.
- **The 1h lane** (VST x02) stays the only validated edge: about PF 1.1–1.2 over a year after cost, but regime-dependent. See `reports/htf-validation-20261007`.

## Reproduce
```
python3 scripts/edge_scan.py --data-dir /tmp/claude-0/wf60 --tfs 1,5,15,30 --holds 5,15,30 \
  --tps 0.3,0.5,0.8,1.2,2.0 --sl-ratios 0.6,1.0,1.5,2.0 \
  --out reports/short-tf-scan-20261007/edge.json --html reports/short-tf-scan-20261007/index.html
```
