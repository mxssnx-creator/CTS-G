# x01 and Signals integration checkpoint — 30 October 2026

This branch combines the ownership, protection, latency and hold work from PR #116 with the history, trailing and scratch fixes from PR #117. It is an integration checkpoint, not evidence of live readiness. The configured x01 symbol cap is 50 and volume factor is 0.05. Existing installed overlays are preserved by the deploy script; this repository setting does not update a running installation.

The merge conflict in the replay lane retains the candle-order correction from #117 and the no-time-limit sentinel from #116. The deployment patch and output hash were regenerated from the merged trader; the restore and pin checks pass. Independent hold-auto parity, independent CTS-A-O signal-source lanes and Last-N auto selection remain incomplete. Exchange ownership and protective-control behavior have not been verified live on this combined code. See the prior reports and the original PR descriptions for additional limitations.

## Integrated paper replay

An eight-symbol, 12-hour prehistory plus 12-hour forward replay used the frozen CTS-A-O candle archive (SHA-256 `e21d39b0b7583685e2fe355f6261e0860deff3057ca5c81b43a6c3cf374e2f03`). It covers 17 October 2026 09:00–21:00 UTC, 1,654 Sets, hourly report checkpoints, a 1.2 Base PF floor and a single 0.12% round-trip paper cost. Automatic holds and automatic history expansion were disabled for this fixed-history test.

At all 12 hourly checkpoints, zero Sets passed Base (0.00%); Base and Main paper PF and their sample counts were unavailable/zero. Three simulated positions closed between checkpoints, all with positive modeled returns; the reported execution PF of 99 is a no-loss sentinel, not a finite estimate. This tiny sample is not exchange execution and does not establish forward profitability or sufficient orders. Neither the whole default grid with automatic holds nor 50-symbol live performance was tested.

Detailed simulated results: [integration-x01-12h-20261030.json.gz](integration-x01-12h-20261030.json.gz). To reproduce, use the commands in `signal-history-audit-20261030.md` with `--pre-hours 12`, `histLookbackBars` 720 and a fresh cache, and run against this integrated branch.
