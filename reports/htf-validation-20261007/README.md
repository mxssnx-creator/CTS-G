# 1h lane (HTF) validation, 2026-10-07

This lane ports CTS-A-O's robust core to CTS-G:
- **RSI-extreme momentum:** 7 variants.
- **1h+4h agreement set:** bb-walk, break-vol ×2, break-atr-2, act-burst-2.5, act-chop, CCI 14/40 ±200 and z 50 ±2.5.
- **Volatility-regime filter:** ATR% must rank in the upper half of the last 336 hours.
- **Wide exits:** TP 2.5–10%, holds of 24–48h, optional trail.

**Data:** BingX public 1h candles, 28 perpetuals, 2024-10-07 → 2026-10-07. The indicator math reproduces CTS-A-O's TypeScript exactly. `scripts/test_htf_engine.py` checks it against vectors produced by running CTS-A-O's `indicators.ts` in Node.

## 1. Selection and out-of-sample (`index.html`, `htf-cost018.json`)

**Method:**
- For each kind, the exit and volRegime setting is picked on half A (2025-10 → 2026-04).
- It is validated on half B (2026-04 → 2026-10) and on the prior year (2024-10 → 2025-10). The prior year was never used to pick exits.
- Costs are 0.18% round trip. PF = gross win / gross loss after cost.

| Family | Half A PF (n) | Half B PF (n) | Prior year PF (n) | Trades/day | Result |
|---|---:|---:|---:|---:|---|
| robust (1h+4h set) | 1.61 (2,949) | **1.36** (2,619) | **1.36** (4,932) | 14.4 | pass |
| rsi-mom | 1.37 (3,444) | **1.17** (3,195) | **1.17** (6,439) | 17.5 | pass |

**Cost sensitivity:**
- At 0.10% cost: B 1.41 / 1.14, prior 1.39 / 1.20.
- At 0.25% cost: B 1.33 / 1.13, prior 1.32 / 1.14.
- Both families pass at every cost level.

**CTS-A-O's last-N 12 gate** lowered the result on our data. With it, robust's prior year fell to PF 0.95, so the lane ships with it off (`htfLastN 0`).

**Caveat:** CTS-A-O chose these kinds on its own data from the same calendar years, though on different symbols. So the prior year here is out-of-sample for our exit choices, but not fully for the choice of kinds.

## 2. Month by month (preset exits, every signal, 28 symbols)

- **Positive months:** 16 of 25.
- **Losing months** can be deep: Dec 2025 PF 0.41, Apr 2026 PF 0.57, Oct 2026 so far PF 0.14.
- **Desk's 12 symbols:** 15 of 25 months positive. Sep 2026 PF 0.85.

## 3. Engine in the loop (`scripts/htf_scenario.py`)

**What it does:**
- Walks hourly with the HtfBook gates, one lot per symbol × kind × side and at most 12 open lots.
- Each close is fed back as live evidence.
- Runs on the 12 desk symbols.

| Window | Variant | Closed | PF | Net Σ % | Trades/day | Positive days* |
|---|---|---:|---:|---:|---:|---|
| 365 d | default gates (window 50) | 877 | **1.10** | +158 | 2.4 | 87/216 |
| 365 d | no gates | 1,958 | 1.10 | +304 | 5.4 | 137/341 |
| 365 d | last-N 12 | 404 | 1.23 | +150 | 1.1 | 51/107 |
| 365 d | window 100 | 1,154 | 1.03 | +53 | 3.2 | 99/260 |
| last 30 d | default | 120 | **0.47** | −153 | 4.0 | 9/24 |

\* Days with at least one close.

**Per family (default gates, 365 d):** robust PF 1.20, rsi-mom PF 0.96.

## Verdict

- The lane earns over a year after cost: about PF 1.1–1.2 on the desk symbols, more on the wider universe.
- Its edge comes from wide winners. Only about 40–50% of days and hours with closes are positive.
- It is regime-dependent. The last 30 days would have lost.

So the lane ships on for VST x02 only, and off on Live x01. There, the live-first gates and the desk live edge guard judge it on real exchange results before it gets more room.

## Reproduce

```
python3 scripts/fetch_htf_klines.py --output <dir>              # 2 years, 30 symbols
python3 scripts/htf_validate.py --data-dir <dir> --out <json> --html <html> [--cost 0.0018]
python3 scripts/htf_scenario.py --data-dir <dir> --days 365 [--no-gates|--last-n 12|--window 100]
```
