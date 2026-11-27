# Direct post-Base execution study

[Open or download the self-contained HTML report](CTS-G-direct-post-Base.html). Downloading and opening it in a browser enables the configuration explorer, hourly settlements and execution CSV export.

## Experiment

Thirty BingX USDT perpetuals are replayed from complete one-minute candles. The two chronological panels each have 24 hours of prehistory and 48 scored hours. Development settlements cover November 18 at 12:00 through November 20 at 12:00 UTC. Holdout settlements cover November 25 at 00:00 through November 27 at 00:00 UTC. Each execution book starts flat. Qualification histories and sizing states are independent between panels.

| Parameter | Values |
| --- | --- |
| Base minimum PF | 1.5, 2.0, 2.5 |
| Native Base lanes | Long 10, 20, 30, 40, 50 × Short 4, 8, 12, 16 × Last 1–5 × both Short signs × both Last signs |
| Standalone Base controls | 5 positive Long, 8 signed Short and 10 signed Last |
| End minimum PF | 2.0, 2.5 |
| End Short | 8±, 12±, 16+, 20+ |
| End Last | 1±, 2±, 3±, 4±, 5± |
| End configurations | All 16 individual gates and all 60 disjoint Short × Last pairs |
| Axis | Off, Pause 1, Pause 2, Prev 4 |
| Block recovery rounds | Off, or count 1 or 2 × ratio 0.10 or 0.20, up to two increments |
| Underlying Sets | Short Range TP 0.4%, Trend TP 1.0%, Reversion TP 0.6%, Breakout trailing 1.2% / 0.5%, EMA Cross short-range 1m TP 0.6%. Each uses its native long and short Set. Ordinary Sets use SL ratio 0.75. |

The simulator reuses the native signal, Set, price-exit, risk and signed-window implementations. A Base order is eligible only at its own entry. The end history contains only earlier, already-settled members of the exact qualified Base lane. Within a signed pair the newest Last observations and preceding Short observations are disjoint. A negative gate uses loss/gain as its qualification PF. All subsequent trade outcomes retain their sign and incur the x02 paper allowance once. Exit and entry on the same timestamp cannot inform one another. Positive hours use the whole scored period. Drawdown is realized, in percentage points of one base order's notional.

The raw exercise compares **38,577,600 configurations per period** across ten exact Sets. Each is a separate equal-base-notional simulation across the symbols. The report also shows direct Base-only controls. The grids do not represent simultaneous positions across alternative configurations.

## Measured results

All six development-selected compound Base choices lost money during the separate holdout. Both end floors selected Last 2+ and gave the same holdout outcomes for these choices.

| Base minimum | End minimums tested independently | Development PF | Holdout PF | Holdout closes | Positive hours / 48 | Realized drawdown, unit-notional pp |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 1.5 | 2.0 and 2.5 | 1.473 | 0.319 | 88 | 3 (6.25%) | 37.59 |
| 2.0 | 2.0 and 2.5 | 1.473 | 0.454 | 68 | 3 (6.25%) | 23.38 |
| 2.5 | 2.0 and 2.5 | 1.614 | 0.300 | 81 | 4 (8.33%) | 33.87 |

The highest-PF primary holdout choice used Reversion short, Base Long 10+ / Short 8+ / Last 2+, end Last 2+, Axis off and one-order Block rounds with a 0.20 increment. Identical entries at flat sizing had PF 0.477. The direct Base-only control had PF 0.461.

Across both periods, 1,403 parameter combinations with sufficient support earned positive net in both. All used a standalone Base gate. Twenty-six had positive net in every daily half. These counts include overlapping configurations. Selecting one candidate per treatment after seeing both periods was a second search. Its four choices were frozen before replaying the next eight hours on the same 30 symbols, November 27 from 00:00 through 08:00 UTC.

| Treatment | Later PF | Closed | Open at cutoff | Positive hours / 8 | Realized drawdown, unit-notional pp |
| --- | ---: | ---: | ---: | ---: | ---: |
| Plain | 0.750 | 13 | 6 | 2 (25.0%) | 3.43 |
| Axis only | 0.350 | 7 | 2 | 1 (12.5%) | 2.80 |
| Block only | 0.750 | 13 | 6 | 2 (25.0%) | 3.43 |
| Axis and Block | 0.454 | 7 | 8 | 1 (12.5%) | 2.24 |

The Block-only selection, Reversion long with Base Short 16− and end Short 20+ / Last 1+, recorded PF 1.144 in development and 1.331 in holdout. Its later book raised zero entries, giving the same result as flat sizing. None of the four treatments established a profitable later result. The eight-hour sample is short.

## Reproduce

From the repository root, with Python 3.10+ (the project dependencies and NumPy) and Node.js 22+:

```sh
mkdir -p /tmp/cts-g-direct-market
tar -xzf reports/direct-base-20261127/bingx-candles.tar.gz -C /tmp/cts-g-direct-market
tar -xzf reports/direct-base-20261127/confirmation-candles.tar.gz -C /tmp/cts-g-direct-market
python3 scripts/direct_base/forward.py --data /tmp/cts-g-direct-market/forward \
  --selection reports/direct-base-20261127/forward-selection.json --out /tmp/cts-g-confirmation.json
python3 scripts/trade_sim.py --direct-post-base \
  --development /tmp/cts-g-direct-market/development \
  --holdout /tmp/cts-g-direct-market/holdout \
  --confirmation /tmp/cts-g-confirmation.json --out /tmp/CTS-G-direct-post-Base.html --jobs 3
python3 scripts/test_direct_base_study.py
```

The companion archive contains both source manifests, their individual SHA-256 candle fingerprints and all 60 symbol-period inputs. The simulator validates each fingerprint, minute clock and OHLCV row. The HTML embeds the native order tapes and the complete Base-to-end membership index. The explorer reconstructs each selected ledger with the same sizing and settlement code as the batch.

[Development selections](development-selection.json) record the parameter choices frozen before holdout scoring. The ranking requires at least 60 closes, 20 in each half and activity in half the clock hours. It maximizes the weaker chronological half's realized PF, then profitable-hour share, lower realized drawdown and number of closes. Stable configuration order resolves exact ties. The published-report test compares the reported winners with direct native gate checks, an independent signed end-window calculation, native Axis rules and the engine's RoundsBook.

The later [frozen choices](forward-selection.json) and [hourly results and order ledger](forward-results.json) cover the four treatment groups in the second search. The report verifies those choices against the same chronological records and requires the later scoring period to begin after holdout.
