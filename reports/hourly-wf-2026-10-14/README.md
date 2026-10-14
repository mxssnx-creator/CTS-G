# Hourly lane walk-forward, 2026-10-14

These are the CTS-A-O 1h pairs, ported exactly (`scripts/test_ao_parity.py`), run over 40 symbols per period. Each period's symbols are the top 40 by 30-day volume before the period starts. Cost is 0.2 % per round trip plus funding.

Reproduce with `scripts/hourly_walkforward.py` on the `.research-data` cache, which `scripts/research_data.py` fetches.

| Period | Dates | Role |
|---|---|---|
| A | 2025-10-11 → 2026-09-26 | selection |
| B | 2024-10-11 → 2025-09-26 | held out for this selection |
| C | 2024-06-01 → 2024-09-25 | held out |
| T | 2026-09-27 → 2026-10-14 | unseen by anyone |

## Hourly Base/Main/Real selection (`walk-robust-r0.json`)

This is A-O's robust 12 pairs: Base over 336 h, pre-window 20 h, last-N 12, 16 seats, trailing cells only.

| Period | Orders/day | PF | Stress PF | Shadow PF (no selection) |
|---|---:|---:|---:|---:|
| A1 | 10.9 | 0.65 | 0.58 | 1.04 |
| A2 | 10.1 | 1.26 | 1.12 | 1.06 |
| B | 11.2 | 0.77 | 0.69 | 1.22 |
| C | 10.9 | 1.08 | 0.97 | 1.08 |
| T | 5.3 | 0.67 | 0.56 | 0.38 |

Picking Sets on their recent PF does worse than taking every signal. In three of the five windows, the selected trades lose while the unselected shadow book wins.

## Cells frozen on A (`static-all30-pf1.2-per2.json`)

This takes the best 2 exit cells per pair on A (PF ≥ 1.2, at least 0.3 trades/day), 25 Sets in total, and trades every one of their signals.

| Period | Orders/day | PF | Stress PF (0.4 %) | PF long | PF short | Weeks at PF ≥ 1 |
|---|---:|---:|---:|---:|---:|---:|
| A (in sample) | 62.7 | 1.32 | 1.18 | 1.31 | 1.32 | 0.47 |
| B | 59.9 | 1.25 | 1.14 | 1.31 | 1.19 | 0.53 |
| C | 64.7 | 1.39 | 1.26 | 1.25 | 1.46 | 0.53 |
| T | 56.5 | **0.29** | 0.26 | 0.40 | 0.24 | 0.33 |

Results:
- The frozen cells hold on both long held-out periods.
- Every variant tested loses in the unseen 17 days of T, which were a slow, choppy decline (alts −9 to −14 %). This held with and without last-N, with the volatility filter, and with the lane guard.
- Even in the good periods only about half the weeks are positive, below the judge's 2/3 rule.
- Nothing passes the go-live judge, so x01 gets no hourly live orders.
