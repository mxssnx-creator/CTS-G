# 200-symbol public audit universe

The earlier public readiness snapshot contained 200 listed contracts, but only
108 matched x01's dynamic crypto USDT eligibility: 90 had excluded `NCCO`, `NCS`
or `NCFX` prefixes, and two were USDC pairs. A successful public-candle check
was therefore not proof of a 200-symbol x01 execution universe.

`scripts/fetch_audit_market.py` now excludes those contracts **before** applying
the 200-symbol limit. It handles boolean and string exchange-open flags in the
same snapshot, removes duplicate ticker rows, and rejects nonfinite or invalid
quote volumes. Ranking is public 24-hour quote volume; it does not reproduce
x01's leverage/one-hour-volatility ranking or access the private account.

The explicit `--top-symbols 200` option fetches exactly 200 eligible contracts
when available. Explicit symbol lists remain supported and must contain distinct,
listed crypto USDT contracts. Metadata describes the actual selection rather
than always claiming a fixed 12-symbol universe. All downloaded minute series
still require complete timestamps and valid OHLCV values; missing candles are
not filled or silently accepted.

For a public-data-only snapshot:

```sh
python3 scripts/fetch_audit_market.py --out audit-market --hours 49 --top-symbols 200 --jobs 6
```

This command does not authenticate an account or place exchange orders. The
49-hour total stays within the project's 72-hour history limit. Selection from
a current market snapshot followed by historical replay is retrospective, not
an out-of-sample profitability test. It cannot establish future profit factor.

Validation: all four `replay-provenance` checks pass. The new regression gives
excluded contracts higher volume than 300 eligible crypto contracts and checks
that exactly 200 distinct eligible contracts remain, in the expected order.
It also covers mixed open-flag types, duplicate rows, closed/missing contracts
and invalid volumes.
