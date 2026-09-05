# Exact five-day public-market research replay

Period: 2026-08-31 00:00 UTC to 2026-09-05 00:00 UTC, end exclusive.
For each XRP-USDT, BCH-USDT and SOL-USDT, all 7,200 one-minute BingX candles
and 60 preceding warmup candles were downloaded and timestamp/OHLCV validated.
No synthetic fallback, credentials, private requests or orders participate.

62,208 independent hypothetical lanes were evaluated: three symbols, eight
existing CTS-G indication kinds, two directions, nine TP values (0.40–0.80%),
nine SL values (0.10–0.50%), and sixteen Base/Block/DCA parameter variants.
Block counts 1–6 are separate sizing experiments using the original-parent
formula, ratio 0.25, cap 2× and favorable-close threshold 0.20%. DCA uses
one/two/three additive 0.25-parent portions and increasing adverse-distance
increments 0.05/0.10/0.20%. These variants do not reproduce live coordination
gates, held Block state or pause-until-positive behavior.

| Symbol | Configs | Without trades | Net positive | Highest net PF* | Net pp* | Close-mark DD pp* |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| XRP-USDT | 20,736 | 2,592 | 0 | 0.735964 | -13.151020 | 16.903634 |
| BCH-USDT | 20,736 | 2,592 | 0 | 0.751440 | -9.715727 | 13.627484 |
| SOL-USDT | 20,736 | 2,592 | 0 | 0.889855 | -1.046291 | 3.841330 |

\* Highest observed PF among configs with at least eight closes in each
chronological 70/30 segment; descriptive selection, not independent qualification.
PnL and drawdown are percent points of original parent notional, not account
returns. Alternative lanes must not be summed as a portfolio. Fees/slippage
are modeled at 0.075% of each executed entry/add/exit notional. Actual account
fees, funding, order-book fills and liquidation are not reconstructed.

Stops precede additions; ambiguous candle exits use SL; gaps use the worse open.
Entry-bar exits and exit-bar re-entry are excluded. Split and terminal positions
close with costs instead of censoring unresolved losses. Drawdown includes
close-mark liquidation equity, not intraminute equity lows. Full HTML includes
all config rows, daily outcomes, fees, sample sizes, PF8/25/75, DDT, holding time,
frequency, volume, filters, pagination and CSV export. There are no winners to
save as positive defaults and no Mainnet qualification.

## Reproduce

```
python3 scripts/fetch_historic_window.py --end 2026-09-05 --days 5 --output /path/data
python3 scripts/replay_five_days.py --data /path/data --output /path/report.html
python3 scripts/test_five_day_replay.py
python3 scripts/test_confirmed_controls.py
node scripts/test_historic_report.mjs /path/report.html
```

NumPy 2.3.5 was used locally; two worker processes kept CPU work off the server.
The original candle hashes are:

- XRP: `a03c0bbe32f06fd6833f49d64f93e7cfc4479817395f29c0a9a9c731b4c2b497`
- BCH: `2a9fb960ff0afd153e04408a588f697ed7c710cb64b14a2bb0d04c68e07bd542`
- SOL: `8917fbf7ff15785647d84817b5d80802bdb801e6cf052074107f75278d0424af`

## Runtime repairs included

Background QA uses copied price/position/closed inputs, preventing dummy probes
from replacing real data. Executed control orders require exact own order IDs
and persisted parent lineage. Cumulative TP/SL prices become incremental fill
prices; repeated fills are idempotent. Failed REST responses do not update costs
or trigger fallback polling. Fill polling is time-based and precedes periodic
position adoption. Warm REST no longer owns the shared statistics lock;
indication snapshot membership is copied before iteration.

Known limits: order-history polling still reads the latest 50 rows and cannot
prove recovery of all older fills. New control fill processing needs remote
exchange acceptance. Dashboard privileged actions/TLS, session-equity guard,
14-/20-day DCA qualification and sustained positive VST results remain separate
unfinished acceptance items. The local HTML URL is blocked by the managed
browser policy; report data reconciliation and interaction contracts were tested
locally, not visually certified in that browser.
