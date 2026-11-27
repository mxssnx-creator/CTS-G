# CTS-G indication sweep, 27 November 2026

The input contains 720,000 consecutive one-minute BingX USDT perpetual candles for 50 fixed symbols. Prehistory is 17–20 November, development is 20–24 November, and the holdout is 24–27 November 2026. Each boundary is 00:00 UTC. The replay validates OHLCV and minute coverage for every symbol before screening. The 14-case roster was selected exclusively from development data. Its SHA-256 is `f1717ac47684fa40bcfc7cd52d84fd995241e0f54759451a05a808131b3b92f2`.

The catalogue covers 834 indicator profiles, including the 72 CTS-G library families, classic reversion, trend, breakout and volume rules, CTS-A-O and CTS-C rules, and 1–30 minute Wilder RSI, PSAR and ADX settings. Each profile receives the same 36 independent Base order geometries from Micro to Wide. A position uses a completed candle, one direction slot per symbol, pessimistic stop precedence, a gap-adjusted stop fill and 0.11% modeled roundtrip cost. The cost stress subtracts four additional basis points per closed trade.

| Frozen profile and geometry | Development PF | Holdout PF | Holdout closes | Profitable holdout hours |
| --- | ---: | ---: | ---: | ---: |
| RSI reclaim 21/30, 15m, TP 5%, SL ×1 | 2.929 | 2.182 | 90 | 9/72 |
| ADX onset 14/18, 30m, TP 2.4%, SL ×0.5 | 3.453 | 1.525 | 161 | 23/72 |
| RSI(2) pullback, 5m schedule, TP 4%, SL ×0.75 | 1.247 | 1.144 | 734 | 36/72 |
| PSAR fast, 15m, TP 4%, SL ×0.5 | 1.402 | 0.817 | 891 | 30/72 |
| Breakout vote 10, 15m schedule, TP 2%, SL ×1 | 2.142 | 0.774 | 180 | 28/72 |

These rows are independent price replays before stage qualification. The native check uses CTS-G signal generation, Set tapes, per-window Auto PF, entry selection and trade settlement. The focused profile has Base Long 10, Short 4 at every stage, both Short signs, and Last 1–5 in both signs. Main, Real and Exec omit Long. History lookup is three days. Each indication and direction is independent. Canonical StageGrid verdicts and ranks are verified on sampled native decisions. An isolated admission search reuses causal tape memberships and ends once a valid Exec path exists. Canonical StageGrid snapshots provide the per-stage report. The sparse native replay walks each opened position through the same exit rules without allocating 50 position lookup indices. Running without `--focused` uses the full stage grid.

The best native development PF was 2.046 for the five-minute VWAP cross candidate, with 96 closes, seven profitable hours out of 96 and 1.329% marked drawdown. No candidate met the requirement of 50% profitable hours in development. The study therefore qualifies no new default indication. A separate full-grid ADX(7) 5m reference run recorded PF 0.836 on 748 development closes and 24 profitable hours.

The development screen took 1,246.2 seconds to prepare its remaining 44 symbol caches after the initial attempt. With all signals prepared, scoring 30,024 cases took 10.3 seconds for development and 15.2 seconds for holdout. A separate BTC replay of 4,320 candles for the 15m Keltner profile made 284 signal-cache entries instead of 4,260, with identical signals at the actual 15m closes. Measured signal time fell from 1.989 to 0.152 seconds.

## Reproduce the 50-symbol study

The feed is a gzip JSON object containing `tickers` with the fixed `sym` names and `candles` with `<symbol>|1` arrays of `t,o,h,l,c,v` objects. The timestamp `t` is the candle opening, in epoch milliseconds. The study uses NumPy and a C++17 compiler. The report contains the exact feed hash and symbol list.

```sh
python3 scripts/indication_sweep.py --feed market.json.gz --work /tmp/cts-sweep --out dev.json --phase development --jobs 6
python3 scripts/select_indication_sweep.py dev.json frozen.json
python3 scripts/indication_sweep.py --feed market.json.gz --work /tmp/cts-sweep --out holdout.json --phase holdout
python3 scripts/confirm_indication_sweep.py --feed market.json.gz --roster frozen.json --out native-dev.json --phase development --focused --jobs 1
python3 scripts/confirm_indication_sweep.py --feed market.json.gz --roster frozen.json --out native-holdout.json --phase holdout --focused --jobs 1
python3 scripts/render_indication_sweep.py dev.json holdout.json native-dev.json native-holdout.json CTS-G-Indikatorstudie.html
```

Settings allow a configuration to evaluate on its own completed UTC boundary using `signalIntervalMinutes`, and set a manual `holdMinutes` when holds are enabled. The timing fields participate in the configuration identity. A profile with omitted timing fields retains its original identity. The engine keeps the existing risk, lane and order logic.

## References

- Wilder RSI and ADX provide the momentum and trend-strength rules. Parabolic SAR adds distinct acceleration steps and ceilings. [TA-Lib RSI](https://ta-lib.org/functions/rsi.html) · [ADX](https://ta-lib.org/functions/adx.html) · [SAR](https://ta-lib.org/functions/sar.html)
- The short RSI pullback and the channel studies cover mean reversion and breakout. [Connors RSI(2)](https://chartschool.stockcharts.com/table-of-contents/trading-strategies-and-models/trading-strategies/rsi-2) · [Donchian](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/price-channels) · [Keltner](https://chartschool.stockcharts.com/table-of-contents/technical-indicators-and-overlays/technical-overlays/keltner-channels)
- The chronological split and sample requirements address selection among many related tests. The 50 markets can share the same price regime. [Bailey et al., Probability of Backtest Overfitting](https://doi.org/10.21314/JCF.2016.322)
- The venue input is the public one-minute USDT swap candle endpoint. [BingX Market Data](https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-market/api-reference.md)
