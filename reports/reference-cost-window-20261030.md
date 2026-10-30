# CTS-G reference replay: cost and open-position accounting

This offline study uses the previously frozen eight-symbol one-minute candle archive for 17 October 2026, 09:00–21:00 UTC. The strategy grid has 1,654 Sets, automatic holds are off, and the selector uses at most one modeled position per symbol. The 0.15% cost is an assumed round-trip allowance, not an observed BingX account rate; no exchange orders or account balances were accessed. Set-window observations can repeat across hourly checkpoints.

| Prior history | Assumed cost | Base validated, average | Base PF | Completed closes | Closed PF | Open at end | Closed + marked PF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 12 h | 0.15% | 0.000% | unavailable | 0 | unavailable | 0 | unavailable |
| 72 h | 0.15% | 0.045% | 1.920 | 2 | 0.010 | 6 | 0.002 |
| 72 h | 0.12% | 0.363% | 1.694 | 16 | 0.031 | 4 | 0.023 |

The Base PF is in-sample, pooled across passing Set windows; the execution figures are forward model outcomes. The 0.15% run had no Main or positive Real passing observations at the checkpoints and one Real Negative observation. Its two completed closes were short, with net returns of -2.175% and +0.0219% of their own notionals. The six remaining short positions had a combined mark of -8.0548% of individual-position notionals after the full assumed cost. The combined PF treats each position as equal notional; it is not account equity, a realized exchange PF, or a claim about current positions. Two closes do not establish a stable PF.

At each of the two 0.15% entries, the Set passed on a pooled Base window (PF 2.397), while the matching Set, symbol and direction had PF 0.311 and 0.857 respectively, below its 1.5 floor. This indicates borrowed evidence, not proof that a strict same-context gate would produce profitable trades.

The corrected simulation holds a symbol occupied through the end of the reporting window when a position is still open, stops scanning at the window boundary, and exports those open positions separately with an end-of-window mark. An independent fixed-exit/cost walk matched both 0.15% closes and found no trailing fills in this sample. Historical audit helpers now read the cost recorded in the simulation instead of silently rebuilding at the overlay default. These changes do not validate live order handling, source-specific Signals, auto holds, protective orders, or profitability.

The JSON companion contains the hourly observations, per-pack and per-exit-type figures, individual simulated results and stage totals. Reproduce with a locally generated trusted cache; pickle files are executable and must not be loaded from untrusted sources:

```sh
python scripts/trade_sim.py --data /path/to/eight-symbol-72h-feed --cache /tmp/ctsg-015-shadow.pkl --symbols 8 --jobs 2 --last-hours 12 --eval-every 3600 --next 15,25 --patch '{"histLookbackBars":4320,"histLookbackAuto":false,"holdEnabled":false,"positionCostPct":0.15}' --out /tmp/ctsg-015
python scripts/audit_reference_stages.py --data /path/to/eight-symbol-72h-feed --cache /tmp/ctsg-015-shadow.pkl --result /tmp/ctsg-015.json
python scripts/audit_reference_fills.py --data /path/to/eight-symbol-72h-feed --cache /tmp/ctsg-015-shadow.pkl --result /tmp/ctsg-015.json
```

The archived feed and generation details are described in `signal-history-audit-20261030.md`. Do not use this result to enable x01: ownership, exchange reconciliation, per-source qualification, and independent forward testing remain unresolved.
