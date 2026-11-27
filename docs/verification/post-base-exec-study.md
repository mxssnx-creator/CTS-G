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
