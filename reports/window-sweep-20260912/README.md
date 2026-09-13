# Seven-day window comparison and confirmed demo results

The [HTML report](cts-g-seven-days.html) covers BCH, SOL and XRP from
2026-09-04 00:00 to 2026-09-11 00:00 UTC, plus one hour of signal warmup.
This is the requested three-symbol benchmark. Ongoing VST tests retain the
separate 20-symbol default, including these three symbols.

## Results and default decision

| Symbol | Alternative config/policy trials | Trials with trades | Net-positive trials | Both segments: ≥8 trades, net positive, CTS-PF >1.02 |
| --- | ---: | ---: | ---: | ---: |
| BCH | 427,680,000 | 90,668,355 | 28,248,683 | 10,006 |
| SOL | 427,680,000 | 15,303,465 | 1,265,393 | 0 |
| XRP | 427,680,000 | 14,914,920 | 6,917,887 | 26 |
| Total | 1,283,040,000 | 120,886,740 | 36,431,963 | 10,032 |

These are alternative research lanes, not simultaneous orders or portfolio
returns. All 48 symbol/indication/direction groups completed. The finite
Cartesian matrix contains 7,920 risk configurations and 3,375 policies per
group: Last-N 5–75/5, deactivation 5–25/5, DDT 1–9h/2h, evaluation after
1/5/10 new closes, and Main/Real windows 5/3, 10/5, 15/10. Axis is off.
The HTML and JSON disclose TP, SL, trailing, Block/DCA and costs in full.

There were 37 groups with enough training trades to select a winner.
**None of those training-selected winners passed the later control segment.**
The additional global rule selected Last-N 55 / deactivation 10 / DDT 9h /
recalc 10 / Main 15 / Real 10 by mean training net. That candidate has
negative training results and fails SOL holdout. It is not a supported default.
The ranking does not reselect after inspecting holdout.

Therefore Base **30**, deactivation **25**, two-day runtime history and
20 VST symbols remain. The requested shared threshold is **strictly >1.02**;
higher configured minima also apply. No logical Set/order cap is introduced.

The highest exploratory row that passes both segments is BCH signals SHORT,
trailing arm/give 1.5%/0.4%, fixed TP disabled, SL 0.4%, Last-N 5,
deactivation 10, DDT 9h, recalc 10, Main/Real 5/3: 17 simulated admitted
trades, +6.7235 percentage points of parent notional, classic PF 2.3442,
CTS cost-PF 1.4038. It was found using the whole sample and is not an
independently validated prediction or a default recommendation.

## Actual retained VST fills

The read-only journal audit at 2026-09-13 00:01 UTC finds 442 complete,
exchange-confirmed X02 demo round trips: 38 wins, 404 losses,
net **−127.30994 demo units**, classic PF **0.0163143**, CTS cost-PF
**−13.1041**. Both PF definitions are shown; the CTS score can be negative.
The retained journal covers 2026-09-09 through 2026-09-12, not a complete
seven-day account history. These results precede this release. They are not
mainnet money and cannot establish that this patch has improved performance.

`vst-results-before.json` contains per-configuration aggregates, with no
credentials or order identifiers. Local unconfirmed closes, partial-only
positions, duplicate fill legs and foreign positions are excluded. Completed
roundtrip accumulators preserve interleaved partial fills.

## Reproduce and inspect

```sh
python scripts/sweep_seven_days.py \
  --data reports/7d-simulation-20260911/data \
  --output reports/window-sweep-20260912 --workers 2
python scripts/sweep_seven_days.py \
  --data reports/7d-simulation-20260911/data \
  --output reports/window-sweep-20260912 --report-only
```

The 48 compressed group files retain exact counts, policy aggregates,
training selections, top rows and digests of every result vector. Source bar
hashes are embedded. `run-provenance.json` preserves the original computation
signature and calculation fingerprint; changing only the report does not
relabel computed results. The report additionally records its renderer hash.
Compiled libraries and prepared signal arrays are reproducible intermediates.

The accelerated 20-column kernel matches an independent scalar oracle across
random event tapes and boundary cases. Same-bar closes cannot admit an entry.
Fees change with every execution/addition. No funding, order-book depth or
exchange latency is reconstructed. Deactivation stops admissions for that
seven-day session; the shadow tape continues. DD/DDT use admitted closed
results; intra-position drawdown is not measured by this policy sweep.

The report passed desktop and mobile browser checks with 135 table rows,
no page errors and no page-level horizontal overflow.

## Deployment verification

Runtime revision `d424a5106820be831494aa7fbdda76fed4761e69` was installed on
X02 VST and prepared for stopped X01. Both persisted profiles are identical
for processing settings: PF 1.02, Base 30, deactivation 25, 2-day history,
20 symbols and zero logical caps. Mainnet PID remains 0 and STOP is preserved.

The two post-restart snapshots show cycle 364 → 471 and replay progress
3/20 → 4/20 symbols in approximately 30 seconds, with zero reported errors.
Both existing logical VST positions have protective controls and reconcile to
one owned exchange position group (`openParity=match`). The initial history
build is still in progress in these snapshots, so no new qualified entry count
is claimed. Subsequent market eligibility and actual fills must be observed;
700 fake-exchange admissions are not 700 remote fills.
