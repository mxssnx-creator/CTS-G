# Signals retrospective diagnostic — 1 November 2026

Offline experiment only. No x02/demo orders, production integration or live acceptance. No configuration is promoted.

Exactly eight archived symbols, 12 hours prehistory and 12 hours modeled trading. No hidden earlier warmup. Window 8 October 2026 12:00–9 October 2026 12:00 UTC (14:00–14:00 Europe/Vienna); the second half is trading. The archive has been studied before: this is **not an untouched holdout**.

All scenarios use per-leg fees on each leg's own notional, adverse entry/exit slippage and a PF floor of 1.3. The maker scenario assumes maker entry; it does not prove maker fills. The smaller book changes allocation, not merely result scaling. Fractions and source identities are frozen in adjacent `.config.json` files.

## Modeled execution, including losses

| Scenario | Contexts | Settled / still open | Closed-return PF | Equity change | Mark drawdown % | Positive / negative / flat hours | Seconds |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline | 2304 | 55 / 2 | 1.025100 | -0.138254 | 0.296264 | 5 / 5 / 2 | 1.270 |
| expanded | 9216 | 91 / 2 | 0.842475 | -1.349718 | 0.342064 | 5 / 7 / 0 | 5.195 |
| cost-stress | 9216 | 59 / 1 | 0.788546 | -1.242690 | 0.288372 | 4 / 6 / 2 | 4.989 |
| maker-hypothesis | 9216 | 101 / 2 | 0.827616 | -1.536524 | 0.406128 | 5 / 7 / 0 | 4.876 |
| smaller-book | 9216 | 48 / 1 | 0.863157 | -0.300857 | 0.075531 | 6 / 6 / 0 | 4.690 |

Initial modeled equity is 1,000 quote units. Marks include estimated close fees/slippage on open positions; no forced end-of-test close hides the remaining exposure. No funding, liquidation, intraminute liquidity or venue netting is modeled. Low drawdown here reflects small modeled exposure and this short window, not a reliable future bound.

**Every scenario ends below starting equity. None is positive every hour.** The baseline's closed PF exceeds one but its end equity is negative. Selecting only passing windows or counting zero-trade hours as wins would hide this failure.

## Expanded grid: final stage evidence

Each exact source/range/TF/symbol/direction/protection/Last-N branch owns separate Base, Main and Real positions. Main and Real re-simulate their admitted entries; they are not filtered copies of Base outcomes. Historical current-window qualification is not future execution profitability.

| Stage | Own passing / population | % passing | Passing through this stage | Insufficient own Last-N | Passing-window PF | All modeled closes | All-close PF |
|---|---:|---:|---:|---:|---:|---:|---:|
| base | 277 / 9216 | 3.005642 | 277 | 7533 | 2.482566 | 58286 | 0.619324 |
| main | 38 / 9216 | 0.412326 | 36 | 8820 | 2.407822 | 6202 | 0.680714 |
| real | 6 / 9216 | 0.065104 | 5 | 9113 | 2.713591 | 1141 | 0.731709 |

Stage totals pool hypothetical overlapping branches and shared price events for diagnosis only; they are not a portfolio PF or independent statistical observations.

Source coverage: 56/384 source/symbol/range/TF combinations lack minimum indicator warmup at trading start; 16 still lack it at the end. Warm indicators can still lack enough own stage closes. The engine reports this insufficiency instead of fabricating prehistory, pooling another lane, or lowering the PF floor.

Detailed grid lines are in `signals-stage-grid-20261101.md`; the compressed scenario files retain every exact symbol/direction context, hourly stage progress, every modeled execution and all 721 equity marks.

## Every modeled trading hour (UTC)

Equity changes include open-position marks. Settled cash alone is not hourly equity profit. Exact boundary convention: candle closes settle in `(from, to]`.

| Scenario | From | To | Equity change | State | Settled orders | Settled cash |
|---|---|---|---:|---|---:|---:|
| baseline | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| baseline | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| baseline | 10-09 02:00 | 10-09 03:00 | 0.205659 | positive | 2 | 0.205659 |
| baseline | 10-09 03:00 | 10-09 04:00 | 0.310590 | positive | 6 | 0.310590 |
| baseline | 10-09 04:00 | 10-09 05:00 | -0.163675 | negative | 3 | 0.017689 |
| baseline | 10-09 05:00 | 10-09 06:00 | 1.229030 | positive | 9 | 1.143884 |
| baseline | 10-09 06:00 | 10-09 07:00 | -1.036406 | negative | 9 | -1.044271 |
| baseline | 10-09 07:00 | 10-09 08:00 | 0.555069 | positive | 5 | 0.656405 |
| baseline | 10-09 08:00 | 10-09 09:00 | 0.331572 | positive | 7 | -0.125904 |
| baseline | 10-09 09:00 | 10-09 10:00 | -0.431621 | negative | 2 | -0.057066 |
| baseline | 10-09 10:00 | 10-09 11:00 | -0.551438 | negative | 6 | -0.304878 |
| baseline | 10-09 11:00 | 10-09 12:00 | -0.587034 | negative | 6 | -0.700412 |
| expanded | 10-09 00:00 | 10-09 01:00 | -0.203167 | negative | 6 | 0.062373 |
| expanded | 10-09 01:00 | 10-09 02:00 | 0.148784 | positive | 9 | 0.151423 |
| expanded | 10-09 02:00 | 10-09 03:00 | -0.037149 | negative | 8 | -0.409064 |
| expanded | 10-09 03:00 | 10-09 04:00 | 0.181491 | positive | 10 | 0.269672 |
| expanded | 10-09 04:00 | 10-09 05:00 | -0.360692 | negative | 6 | -0.149917 |
| expanded | 10-09 05:00 | 10-09 06:00 | 0.747175 | positive | 11 | 0.771393 |
| expanded | 10-09 06:00 | 10-09 07:00 | -0.416723 | negative | 9 | -0.577384 |
| expanded | 10-09 07:00 | 10-09 08:00 | 0.295240 | positive | 10 | 0.393714 |
| expanded | 10-09 08:00 | 10-09 09:00 | 0.148169 | positive | 6 | -0.117099 |
| expanded | 10-09 09:00 | 10-09 10:00 | -0.726680 | negative | 5 | -0.514025 |
| expanded | 10-09 10:00 | 10-09 11:00 | -0.848479 | negative | 6 | -0.654433 |
| expanded | 10-09 11:00 | 10-09 12:00 | -0.277688 | negative | 5 | -0.233666 |
| cost-stress | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| cost-stress | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| cost-stress | 10-09 02:00 | 10-09 03:00 | 0.240355 | positive | 1 | 0.206016 |
| cost-stress | 10-09 03:00 | 10-09 04:00 | 0.150524 | positive | 5 | 0.188780 |
| cost-stress | 10-09 04:00 | 10-09 05:00 | -0.410493 | negative | 6 | -0.113895 |
| cost-stress | 10-09 05:00 | 10-09 06:00 | 0.790203 | positive | 9 | 0.638392 |
| cost-stress | 10-09 06:00 | 10-09 07:00 | -0.751806 | negative | 8 | -0.900509 |
| cost-stress | 10-09 07:00 | 10-09 08:00 | 0.517895 | positive | 11 | 0.713400 |
| cost-stress | 10-09 08:00 | 10-09 09:00 | -0.683972 | negative | 5 | -0.964495 |
| cost-stress | 10-09 09:00 | 10-09 10:00 | -0.433149 | negative | 6 | -0.293140 |
| cost-stress | 10-09 10:00 | 10-09 11:00 | -0.153506 | negative | 5 | 0.039537 |
| cost-stress | 10-09 11:00 | 10-09 12:00 | -0.508740 | negative | 3 | -0.555210 |
| maker-hypothesis | 10-09 00:00 | 10-09 01:00 | -0.288243 | negative | 7 | -0.037703 |
| maker-hypothesis | 10-09 01:00 | 10-09 02:00 | 0.223784 | positive | 9 | 0.218923 |
| maker-hypothesis | 10-09 02:00 | 10-09 03:00 | -0.080625 | negative | 11 | -0.385671 |
| maker-hypothesis | 10-09 03:00 | 10-09 04:00 | 0.146755 | positive | 11 | 0.183068 |
| maker-hypothesis | 10-09 04:00 | 10-09 05:00 | -0.300692 | negative | 6 | -0.104917 |
| maker-hypothesis | 10-09 05:00 | 10-09 06:00 | 1.334945 | positive | 14 | 1.235944 |
| maker-hypothesis | 10-09 06:00 | 10-09 07:00 | -0.178359 | negative | 7 | -0.155964 |
| maker-hypothesis | 10-09 07:00 | 10-09 08:00 | 0.332706 | positive | 9 | 0.296272 |
| maker-hypothesis | 10-09 08:00 | 10-09 09:00 | 0.101671 | positive | 7 | 0.052137 |
| maker-hypothesis | 10-09 09:00 | 10-09 10:00 | -0.888720 | negative | 6 | -0.862888 |
| maker-hypothesis | 10-09 10:00 | 10-09 11:00 | -1.248649 | negative | 8 | -1.020109 |
| maker-hypothesis | 10-09 11:00 | 10-09 12:00 | -0.691098 | negative | 6 | -0.627911 |
| smaller-book | 10-09 00:00 | 10-09 01:00 | -0.100142 | negative | 6 | 0.031187 |
| smaller-book | 10-09 01:00 | 10-09 02:00 | 0.045206 | positive | 7 | 0.003270 |
| smaller-book | 10-09 02:00 | 10-09 03:00 | -0.089889 | negative | 5 | -0.231149 |
| smaller-book | 10-09 03:00 | 10-09 04:00 | 0.030552 | positive | 4 | 0.082420 |
| smaller-book | 10-09 04:00 | 10-09 05:00 | -0.090682 | negative | 0 | 0.000000 |
| smaller-book | 10-09 05:00 | 10-09 06:00 | 0.341664 | positive | 8 | 0.280979 |
| smaller-book | 10-09 06:00 | 10-09 07:00 | -0.205508 | negative | 4 | -0.226309 |
| smaller-book | 10-09 07:00 | 10-09 08:00 | -0.054882 | negative | 3 | -0.121618 |
| smaller-book | 10-09 08:00 | 10-09 09:00 | 0.016414 | positive | 1 | 0.062377 |
| smaller-book | 10-09 09:00 | 10-09 10:00 | 0.115589 | positive | 6 | 0.127167 |
| smaller-book | 10-09 10:00 | 10-09 11:00 | -0.383857 | negative | 3 | -0.341068 |
| smaller-book | 10-09 11:00 | 10-09 12:00 | 0.074678 | positive | 1 | 0.111176 |

## Limits and release gate

Only EMA cross, MACD cross, RSI reversal, EMA trend, RSI momentum and Donchian short/medium ports are implemented. This is not the full CTS-A-O catalog, MFE fitting, timeframe-distance scaling, DCA/-Active Signals or the ordinary runtime's coordinator adapter. Model orders/positions are 1:1, not exchange stacking or partial execution. Separate trader fixtures cover those existing production paths; venue acceptance remains untested.

Do not merge into a live release until frontend build/render checks, complete Signal integration, independent trailing-path reconstruction, forward/out-of-sample evidence and x02 order/fill/protection reconciliation pass. Positive PF and hourly profits cannot be enforced by changing reported results.
