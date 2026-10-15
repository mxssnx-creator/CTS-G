# Hourly lane walk-forward, 2026-10-14

These are the CTS-A-O 1h pairs, ported exactly (`scripts/test_ao_parity.py`), run over 40 symbols per period. Each period's symbols are the top 40 by 30-day volume before the period starts. Cost is 0.2 % per round trip plus funding.

The tables use return-based PF: `1 + 0.10 × average cost/funding-net return percent / 0.20`. They were recalculated from the archived net-return sums and close counts on 2026-10-15. The original JSON files retain their legacy classic PF; the canonical values are in [hourly-archive-return-pf.json](../pf-contract-audit-20261015/hourly-archive-return-pf.json), reproduced with `scripts/rebase_hourly_archive_pf.py`. The original admitted trades are unchanged. Raw candle/trade caches are unavailable in this checkout, so this is not a rerun of the corrected admission policy.

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
| A1 | 10.9 | 0.6318 | 0.5318 | 1.0273 |
| A2 | 10.1 | 1.1911 | 1.0911 | 1.0441 |
| B | 11.2 | 0.7548 | 0.6548 | 1.1681 |
| C | 10.9 | 1.0736 | 0.9736 | 1.0567 |
| T | 5.3 | 0.7567 | 0.6567 | 0.2600 |

Picking Sets on their recent PF does worse than taking every signal. In three of the five windows, the selected trades lose while the unselected shadow book wins.

## Cells frozen on A (`static-all30-pf1.2-per2.json`)

The original selection took the best 2 exit cells per pair on A (legacy classic PF ≥ 1.2, at least 0.3 trades/day), 25 Sets in total, and traded every one of their signals. Return PF below describes those same trades; it does not claim they were selected using the corrected gate.

| Period | Orders/day | PF | Stress PF (0.4 %) | PF long | PF short | Weeks at PF ≥ 1 |
|---|---:|---:|---:|---:|---:|---:|
| A (in sample) | 62.7 | 1.2506 | 1.1506 | 1.2631 | 1.2349 | 0.47 |
| B | 59.9 | 1.2472 | 1.1472 | 1.2819 | 1.2033 | 0.53 |
| C | 64.7 | 1.3305 | 1.2305 | 1.1840 | 1.4191 | 0.53 |
| T | 56.5 | **−0.0876** | −0.1876 | 0.0035 | −0.1244 | 0.33 |

Results:
- The frozen cells hold on both long held-out periods.
- Every variant tested loses in the unseen 17 days of T, which were a slow, choppy decline (alts −9 to −14 %). This held with and without last-N, with the volatility filter, and with the lane guard.
- Even in the good periods only about half the weeks are positive, below the judge's 2/3 rule.
- Nothing passes the go-live judge, so x01 gets no hourly live orders.
