# Edge scan, 2026-10-07

**Data:** 12 BingX perpetuals, 1m bars, 14 UTC days (2026-09-22 → 2026-10-06).

## 1. Engine walk-forward grid

The engine was run on 13 daily windows, 24h each, with a 24h warm-up before each window.

Setups tested (21 in total):
- Strategies: Normal + Trailing, or Trailing only.
- Axis filter: none, any, last, cont, or pause.
- Micro: on or off.
- Plus the baseline.

| Result | Value |
|---|---|
| Return per day | −22% (best) to −27% (worst) |
| Days positive | 0 of 13 for every setup |
| Cost-PF | 0.83 – 0.86 |
| Positive hours | about 36% |
| Trades | about 3,000 – 3,900 over 13 days |

Least-bad setup: Axis "last" with Micro off. It loses less mainly because it trades less.

## 2. Signal family scan (`scripts/edge_scan.py`)

**Families:** momentum, reversion, breakout, fade-breakout, squeeze-breakout, trend-pullback, volume-follow and volume-fade, each with its own parameter grid.

**Exit grid:**
- TP 0.3 – 1.5%
- SL:TP ratio 0.6 – 2.0
- time stop of 1h or 4h

**Walk-forward method:** each day, take the 3 configurations with the best PF on all earlier days, then trade only those on the next day.

**Survival rule (fixed before the run):**
- positive on at least 4 of the last 6 days;
- total net result above 0;
- at least 30 trades per day;
- at least 55% of traded hours positive.

| Cost per trade | Survivors | Best family out-of-sample |
|---|---|---|
| 0.18% (PositionCost) | none | reversion: PF 1.055, +53% summed over 533 trades, 6/11 days positive, last 6 days 2/6 |
| 0.10% (real fee) | none | reversion: PF 1.18, 7/11 days positive, last 6 days 3/6 |

Breakout, trend-pullback and volume-follow had no configuration that was positive in-sample at 0.18% cost. Momentum and fade-breakout lost out-of-sample at both cost levels.

## Conclusion

No signal family has a stable edge after costs on this data.

Reversion is the only lead. It fades a 2.5–3.5σ move over 15–240 minutes, with a 1.5% TP and a 4h hold. Its results are concentrated on a few days and it is not consistent recently. It needs more data before it is wired into the engine.

The desk live edge guard (`liveEdgeGuard`) keeps losses at probe size while a desk's own live results are net negative.

## Reproduce

```
python3 scripts/edge_scan.py --data-dir <windows> --cost-pct 0.18 \
  --out reports/edge-scan-20261007/edge-cost018.json --html reports/edge-scan-20261007/index.html
```
