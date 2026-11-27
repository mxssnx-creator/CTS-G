# CTS-G direct post-Base simulation

The historical study runs on the test branch. It uses the CTS-G Set builder,
indication replay, Base qualification, simulated exits, Axis Sets and Set
Block rules. The entry path is **Base → final Short and Last → simulated
execution**.

| Stage | Windows | Minimum PF |
| --- | --- | --- |
| Base | Long 10, 20, 30, 40, 50. Short 4, 8, 12, 16 with both signs. Last 1–5 with both signs. | 1.5, 2.0, 2.5 |
| Final | Positive Short 8, 12, 16, 20. Negative Short 8, 12. Last 1–5 with both signs. | 2.0, 2.5 |

Base uses CTS-G’s three disjoint blocks and its positive combined PF condition.
Each Base path admits only positions that met that path’s rules at their
entry. A final Short block immediately precedes its Last block on this
derived history. Each block checks its own signed PF. Negative PF is
absolute losses divided by gains for qualification. The position direction
and realized P&L are preserved. A result at the current entry timestamp
becomes visible on a later decision.

Each period runs all 360 isolated final-window cases, plus the six portfolios
that can select any of the 60 final windows. Independent Base paths retain
their full identities even when sharing a calculation. The HTML lists all
400 Base lanes, every native Set in the replay scope, coverage, hourly
results and settled results by exact path and symbol.

## Market data and strategy scope

| Period | Prehistory begins UTC | Forward decisions begin UTC | End UTC |
| --- | --- | --- | --- |
| Development | 2026-11-17 12:00 | 2026-11-19 12:00 | 2026-11-20 12:00 |
| Validation | 2026-11-24 00:00 | 2026-11-26 00:00 | 2026-11-27 00:00 |

The study uses 30 BingX USDT perpetuals from the same archived venue universe.
Each symbol has 4,320 consecutive 1-minute candles per period. Inputs are
hashed in the generated report. The research preset keeps the eight ordinary
packs, limits Signals to 5 minutes and uses selected native variants for
Reversion, Flow and Multi. The paper fee policy resolves to a 0.110% round-trip
cost. Base reads up to 1,920 minutes, so early events are subject to finite
prehistory.

Axis Sets sizes an already qualified position on its own Base-derived
history. Block variants use separate ledgers, the selected parent’s current
Base and final path, its native target-room rule and original stop/exit.
The matrix varies count distance, add ratio, relation and Shared versus
Additive geometry. Identical effective sizes are comparable execution rules
on the same positions. The portfolio compares one unit of starting notional
per parent. Drawdown uses settled equity, with marked holdings and their
add quantities reported separately. Venue fees beyond the modeled costs,
funding, order book liquidity, lot rounding and partial-fill timing are
outside this replay.

The report selects Axis and Block settings on development and evaluates those
same settings on validation. Settled P&L excludes holdings. Hour success is
net-positive hours divided by all decision hours. Gains divided by absolute
losses gives settled Execute PF.

## Reproduce

Run the replay for each archive. Each directory contains one JSON file per
symbol with rows in `[open_ms, open, high, low, close, volume]` order. A
`_meta.json` mapping symbols to SHA-256 digests is supported. The script
requires NumPy and the existing Python runtime dependencies. Its local
shadow cache is bound to the data, settings and engine hashes. Use a cache
generated locally by this runner.

```sh
python3 scripts/test_post_base_exec.py
python3 scripts/post_base_exec.py --data development --cache development.pkl.gz \
  --patch presets/post-base-exec-study.json --out development.json --label development
python3 scripts/post_base_exec.py --data validation --cache validation.pkl.gz \
  --patch presets/post-base-exec-study.json --out validation.json --label validation
python3 scripts/render_post_base_exec.py --development development.json \
  --validation validation.json --out CTS-G-Post-Base-Exec-PF.html
```

## Measured results

Each period completed 684 cases across 5,456 native Sets and 30 symbols.
The baseline rows below permit all 60 final-window choices with Axis
and Block off. Profitable hours use all 24 forward hours.

| Base minimum PF | Final minimum PF | Development PF | Closes | Profitable hours | Validation PF | Closes | Profitable hours |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1.5 | 2.0 | 0.695 | 865 | 5/24 (20.8%) | 0.692 | 721 | 7/24 (29.2%) |
| 1.5 | 2.5 | 0.697 | 862 | 6/24 (25.0%) | 0.695 | 726 | 8/24 (33.3%) |
| 2.0 | 2.0 | 0.649 | 801 | 5/24 (20.8%) | 0.734 | 699 | 9/24 (37.5%) |
| 2.0 | 2.5 | 0.649 | 791 | 5/24 (20.8%) | 0.744 | 708 | 9/24 (37.5%) |
| 2.5 | 2.0 | 0.678 | 770 | 8/24 (33.3%) | 0.709 | 627 | 9/24 (37.5%) |
| 2.5 | 2.5 | 0.690 | 753 | 8/24 (33.3%) | 0.713 | 625 | 8/24 (33.3%) |

Across the 360 isolated signed-window cases, 0 reached PF above 1
with at least 30 closes in each period.

The Axis and Block selection rule confirmed 0 settings. The
rule requires a development PF lift over the matching baseline, a settled
drawdown within 110% of the baseline, enough resizes or adds, and PF above
1 in both periods with the same criteria applied on validation.

Verification included four study regressions, 34 native stage-grid checks,
31 stage regressions and 10 Block regressions. Hourly, Set-path and symbol
close counts reconcile for every reported case.

## Forward fill and release follow-up

The follow-up resolves the entry from the opening immediately after the
completed signal candle. It uses the native exit state machine, including
same-candle stop priority and stop gaps. Fee debit follows each simulated fill's
notional, with adverse slippage in the fill price. Signed Short and Last
windows keep their independent paths and the position's original direction.
The extra proof gate checks the most recent closed, after-cost positions from
the same Base path in their original direction before ranking an entry.

The smaller 132-Set study scans two five-minute packs, Bollinger bounce and
MACD crossover. It covers exact Sets, pack-level portfolios, seven market
conditions, all requested PF pairs and three levels of directional proof.
Of 9,882 cases, 1,080 closed at least 30 positions in every one of five scanned
windows. Zero of those reached PF above 1 in all five. The first two windows
were used to choose the six frozen configurations. Four older periods were
used as retrospective checks and the Nov 27 window starts at the research
cutoff. All eight executions use 30 symbols and 48 hours of prehistory each.
The research and Nov 27 inputs match their archive checksums. The older
archives have recorded hashes from this run and no source checksum manifest.

| Scored window (UTC) | Step 10 PF | Closes | Profitable hours | Combined control PF | Closes | Profitable hours |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Nov 19 12:00–Nov 20 12:00 | 6.075 | 34 | 8/24 | 0.665 | 582 | 5/24 |
| Nov 26 00:00–Nov 27 00:00 | 1.994 | 37 | 8/24 | 0.660 | 405 | 8/24 |
| Nov 10 00:00–Nov 11 00:00 | 0.911 | 17 | 2/24 | 0.490 | 345 | 3/24 |
| Nov 12 00:00–Nov 13 00:00 | 2.633 | 17 | 6/24 | 0.913 | 362 | 8/24 |
| Nov 15 00:00–Nov 16 00:00 | 0.270 | 12 | 1/24 | 0.671 | 517 | 8/24 |
| Nov 24 00:00–Nov 25 00:00 | — | 0 | 0/24 | 1.007 | 521 | 10/24 |
| Nov 27 00:00–08:00 | — | 0 | 0/8 | 0.501 | 99 | 3/8 |
| Nov 27 00:00–08:00, extra 1 bp per side | — | 0 | 0/8 | 0.363 | 107 | 3/8 |

The step-10 case uses Base 1.5, final 2.0, and directional PF 1.05 over 24
closes. The combined control uses Base 2.0 and final 2.5. The higher-threshold
step-8 case with directional proof reached PF 4.301 and 1.566 on the research
periods and settled zero positions on Nov 27. The fresh control's best Axis
variant resized one parent and reached PF 0.509. Block recorded no fresh adds.
Across 54 Axis and Block combinations per frozen case, none sustained PF
above 1 in every independent window and the cost stress test. The frozen
release check rejects all six cases. This branch is suitable for continued
offline testing. Production promotion remains blocked by the forward results.

Run the narrow study with NumPy and the Python runtime dependencies installed.
Use one invocation per period, with `--hours 8` for the recent
market window and `--additional-slippage-bps 1` on that same
archive for stress. Use a separate cache path for the stress replay. Pass
`--market-filters --exact-sets` without a selection for a full scan.
The checker exits with status 1 when the frozen configurations fail the
historical release criteria. It rejects overlapping evaluation periods,
missing sample support, mismatched stress data and inconsistent settlement
totals. Account-specific commissions, liquidity, lot sizes and funding still
need venue evidence for production consideration.

```sh
python3 scripts/post_base_forward.py --data archive --label period \
  --cache period.pkl.gz --out period.json \
  --patch presets/post-base-forward-study.json \
  --selection presets/post-base-forward-selection.json --exact-sets --axis-block
python3 scripts/check_post_base_forward.py --selection presets/post-base-forward-selection.json \
  --research research-a.json research-b.json \
  --evaluate retro-a.json retro-b.json retro-c.json retro-d.json fresh.json --stress stress.json
python3 scripts/render_post_base_forward.py --selection presets/post-base-forward-selection.json \
  --research research-a.json research-b.json \
  --evaluate retro-a.json retro-b.json retro-c.json retro-d.json fresh.json --stress stress.json \
  --scan scan-a.json scan-b.json scan-c.json scan-d.json scan-e.json --out forward.html
```
