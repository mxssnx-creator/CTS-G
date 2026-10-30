# Symbol-and-side replay experiment

This is research code only. It does not implement scoped histories in the live engine or replace its pooled stage calculations. It evaluates each Set on paper trades from the same symbol and direction, requiring at least 12 such closes. It refreshes rankings hourly, retains the existing highest-confidence pack choice and one-position-per-symbol execution model, and optionally rejects a Set when its modeled initial stop distance plus assumed cost exceeds 3%. No account or exchange was accessed.

The frozen reference used eight symbols, 72 hours of prehistory and a 12-hour forward window on 17 October 2026, 09:00–21:00 UTC. Its CTS-A-O candle feed SHA-256 was `e21d39b0b7583685e2fe355f6261e0860deff3057ca5c81b43a6c3cf374e2f03`. The capped trailing grid contained 3,036 Sets; automatic holds were off and round-trip PositionCost was an assumed 0.15%, not the account's observed maker/taker fees or measured slippage.

With the optional 3% modeled stop-plus-cost filter, there were 14 modeled closes with PF 0.334 and no positions left open within the window. The entry gate had at least 18 same-context closes for each recorded entry. The extra RSI-reversion pack had seven closes at PF 0.188; the Donchian pack had four positive closes and no losses, so its PF is undefined, not a meaningful proof of high PF. Two General closes and one Short Range close comprise the rest. Samples are small, Set histories overlap, and the entries are not independent experiments. A signal closing exactly at the report boundary is excluded because there is no forward observation for it.

An audit reconstructed every recorded signal, hourly scoped gate and ranking, and bar-by-bar modeled exit with no mismatches. It uses the same gate functions as the experiment, so it is not an independent implementation of validation. OHLC bars cannot reveal intrabar order, and modeled stop fills do not guarantee live fill prices. Without the optional stop filter, the experiment produced the same aggregate close and mark returns with different selected Sets; the matching aggregate is not evidence that the sets or risks are equivalent. These results differ from the parent pooled replay in both evidence scope and gate refresh cadence, so they do not isolate the effect of a single change.

The source feed and generated cache are not committed here. To reproduce, use the exact trusted feed with the checksum above and generate a fresh cache for this exact grid; a cache from another grid with the same Set count is not sufficient. `prepare_signal_reference.py` converts the CTS-A-O feed, and `trade_sim.py` builds the trusted shadow cache and hourly reference. Then run:

```bash
python scripts/experimental_scoped_replay.py --data DATA_DIR --cache TRUSTED_SHADOW.pkl --reference HOURLY_REFERENCE.json --out scoped.json --min-context 12 --max-modeled-stop-loss-pct 3
python scripts/experimental_scoped_audit.py --data DATA_DIR --cache TRUSTED_SHADOW.pkl --result scoped.json
```

The 3% condition is a modeled initial price-stop distance plus one cost allowance, not a guarantee on realized trade loss, an account drawdown limit, or coverage of DCA, recovered positions, or venue protection. More windows, live fee and slippage measurements, full stage isolation, x02 exchange verification and profitable forward evidence remain necessary before live use.
