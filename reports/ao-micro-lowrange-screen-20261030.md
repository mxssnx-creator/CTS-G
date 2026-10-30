# CTS-A-O Micro / Low-Range candidate screen — 30 October 2026

This is a preliminary offline signal screen, not a simulation of the Base → Main → Real pipeline or live exchange performance. It uses eight BingX 1-minute series, 1,440 contiguous bars each: 29 October 03:07–15:06 UTC prehistory and 29 October 15:07–30 October 03:06 UTC forward. The first 100 bars warm up signals. These data have appeared in earlier analysis, so the forward period is not an untouched holdout.

Each candidate runs independently, samples every third minute, allows one simulated position per symbol and candidate, and uses 0.36% TP, 0.24% SL, a 60-minute time exit, stop-first ties and a 0.12% round-trip cost once. Positions crossing the split are excluded from both scores. Open positions at the end are not scored. Equal-notional trade returns are pooled across symbols. It does not apply Base/Main/Real validation, leverage, exchange fills, slippage, allocation, or trailing stops. PF 99 denotes a positive sample with no losses; it is not a finite estimate.

A candidate was selected only if the prehistory had at least 10 closes and PF ≥ 1.2. **None of the 19 candidates met both conditions.** Small samples and this one exit shape cannot establish that the families do not work in other ranges or coordinations. No new indication was activated on this evidence.

| Candidate | Prehistory closes / PF | Forward closes / PF | Open at end |
|---|---:|---:|---:|
| ao_axisrev_n5_t1 | 27 / 0.6114 | 16 / 0.8571 | 1 |
| ao_axisrev_n8_t1 | 90 / 0.4552 | 103 / 0.538 | 1 |
| ao_cflbrk_n5_t1 | 65 / 0.5291 | 109 / 0.5176 | 2 |
| ao_cflbrk_n8_t1 | 64 / 0.3593 | 90 / 0.6986 | 2 |
| ao_cfltrend_n5_t1 | 147 / 0.3918 | 266 / 0.6705 | 3 |
| ao_cfltrend_n8_t1 | 150 / 0.3801 | 276 / 0.6733 | 3 |
| ao_cmpx_n5_t1 | 37 / 0.5708 | 61 / 0.6613 | 1 |
| ao_cmpx_n8_t1 | 37 / 0.5708 | 61 / 0.6613 | 1 |
| ao_laguerre_n5_t1 | 74 / 0.3325 | 94 / 0.4055 | 2 |
| ao_laguerre_n8_t1 | 55 / 0.4723 | 79 / 0.3792 | 2 |
| ao_regime_n5_t1 | 109 / 0.3891 | 167 / 0.508 | 0 |
| ao_regime_n8_t1 | 102 / 0.3566 | 135 / 0.4942 | 0 |
| ao_ultimate_n5_t1 | 127 / 0.5159 | 201 / 0.4081 | 4 |
| ao_ultimate_n8_t1 | 72 / 0.5107 | 105 / 0.4103 | 3 |
| lr_break | 1 / 99 | 1 / 99 | 0 |
| lr_fade | 2 / 99 | 4 / 0.0 | 0 |
| lr_inside | 3 / 0.5036 | 5 / 2.6667 | 0 |
| lr_nr7 | 97 / 0.4306 | 179 / 0.506 | 1 |
| lr_trendbreak | 4 / 0.6667 | 9 / 1.3333 | 0 |

The existing CTS-G indication adapter registers 13 CTS-A-O-derived families, but can aggregate several variants into one pack. It does not establish independent per-indication, range, and coordination lanes. CTS-A-O's own Low-Range window screen reported initial validated-direction PF 2.905 versus forward PF 0.683 on 80 settled trades; that separate study used a different sample and must not be combined with these numbers.

Reproduce with `python3 scripts/ao_micro_screen.py --data DATA_DIRECTORY --out RESULT.json --jobs 4` after supplying the exact bars below. The source candles are not bundled here.

| Input | SHA-256 |
|---|---|
| ADA-USDT.json | f72521097075b046a5f0f7fdf9a43b39786af16e91d329cf82b71dff0f8592b0 |
| BCH-USDT.json | 9a3de921b4fc52eff3c24d2930a1d43883b025d6e30351881a9c7761b509b698 |
| BNB-USDT.json | 7349748cac24d3042ed80ff5ef6be5f19fbb279b4633855b6f42f05cf24094f1 |
| BTC-USDT.json | b6db318a8b30926f59c5a6e7dc326882011659336129e76f1286fcb2cb2c3f1e |
| DOGE-USDT.json | 5d8a3cfd5d4861c9a05ef0fd988a913ae8a7eab4891e2b74e1deab0477d36180 |
| ETH-USDT.json | f7e4b23f9ab8cd0643555742c21cfc23b281a4994ca3ff000ffca197e29e7920 |
| SOL-USDT.json | f7ec16d12891ae58e5b7e8e9e4bfe360b4b1e6bc5ec8c87448f2b6eb86268ff5 |
| XRP-USDT.json | 98b57fb870a9a5f30c5ed015f32dc0caf535315b0e8ee4630072e400b6af9524 |
