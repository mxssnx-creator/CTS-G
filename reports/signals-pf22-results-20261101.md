# Signals retrospective diagnostic — 1 November 2026

Offline experiment only. No x02/demo orders, production integration or live acceptance. No configuration is promoted.

Exactly eight archived symbols, 12 hours prehistory and 12 hours modeled trading. No hidden earlier warmup. Window 8 October 2026 12:00–9 October 2026 12:00 UTC (14:00–14:00 Europe/Vienna); the second half is trading. The archive has been studied before: this is **not an untouched holdout**.

Per-leg fees use each leg's own notional, with adverse entry/exit slippage. Configured PF floors: [2.2]. A maker scenario is an assumed fill, not proof of a maker execution. Fractions and source identities are frozen in adjacent `.config.json` files. A `-frozen` case admits only exact contexts qualifying before trading; dynamic cases can qualify later. All ongoing gates remain active.

## Modeled execution, including losses

| Scenario | Contexts | Settled / still open | Closed-return PF | Equity change | Mark drawdown % | Positive / negative / flat hours | Seconds |
|---|---:|---:|---:|---:|---:|---:|---:|
| baseline | 2304 | 31 / 1 | 0.732791 | -0.790545 | 0.186170 | 5 / 5 / 2 | 1.332 |
| cost-stress | 9216 | 17 / 0 | 0.243187 | -1.825319 | 0.187921 | 2 / 5 / 5 | 4.553 |
| expanded | 9216 | 61 / 1 | 0.706179 | -1.617727 | 0.266062 | 5 / 7 / 0 | 4.680 |
| maker-hypothesis | 9216 | 65 / 1 | 0.804673 | -1.106794 | 0.247806 | 6 / 6 / 0 | 4.438 |
| smaller-book | 9216 | 41 / 1 | 0.652716 | -0.676871 | 0.105180 | 5 / 7 / 0 | 4.650 |

Initial modeled equity is 1,000 quote units. Marks include estimated close fees/slippage on open positions; no forced end-of-test close hides the remaining exposure. No funding, liquidation, intraminute liquidity or venue netting is modeled. Low drawdown here reflects small modeled exposure and this short window, not a reliable future bound.

Net-positive ending equity: 0/5 scenarios. Positive every hour: 0/5. Empty/flat hours are not wins. Passing historical windows are not a portfolio profit guarantee.

## expanded: final stage evidence

Each exact source/range/TF/symbol/direction/protection/Last-N branch owns separate Base, Main and Real positions. Main and Real re-simulate their admitted entries; they are not filtered copies of Base outcomes. Historical current-window qualification is not future execution profitability.

| Stage | Own passing / population | % passing | Passing through this stage | Insufficient own Last-N | Passing-window PF | All modeled closes | All-close PF |
|---|---:|---:|---:|---:|---:|---:|---:|
| base | 137 / 9216 | 1.486545 | 137 | 7533 | 4.349447 | 58286 | 0.619324 |
| main | 12 / 9216 | 0.130208 | 11 | 8993 | 3.365671 | 3457 | 0.708104 |
| real | 2 / 9216 | 0.021701 | 1 | 9173 | 3.308734 | 430 | 0.882249 |

Stage totals pool hypothetical overlapping branches and shared price events for diagnosis only; they are not a portfolio PF or independent statistical observations.

Source coverage: 56/384 source/symbol/range/TF combinations lack minimum indicator warmup at trading start; 16 still lack it at the end. Warm indicators can still lack enough own stage closes. The engine reports this insufficiency instead of fabricating prehistory, pooling another lane, or lowering the PF floor.

Detailed grid lines are in `signals-pf22-stage-grid-20261101.md`; the compressed scenario files retain every exact symbol/direction context, hourly stage progress, every modeled execution and all 721 close equity marks plus opening/admission marks.

## Every modeled trading hour (UTC)

Equity changes include open-position marks. Settled cash alone is not hourly equity profit. Exact boundary convention: candle closes settle in `(from, to]`.

| Scenario | From | To | Equity change | State | Settled orders | Settled cash |
|---|---|---|---:|---|---:|---:|
| baseline | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| baseline | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| baseline | 10-09 02:00 | 10-09 03:00 | 0.102830 | positive | 1 | 0.102830 |
| baseline | 10-09 03:00 | 10-09 04:00 | 0.141284 | positive | 3 | 0.141284 |
| baseline | 10-09 04:00 | 10-09 05:00 | -0.074151 | negative | 1 | 0.107214 |
| baseline | 10-09 05:00 | 10-09 06:00 | 0.429647 | positive | 7 | 0.459136 |
| baseline | 10-09 06:00 | 10-09 07:00 | -1.238659 | negative | 7 | -1.449512 |
| baseline | 10-09 07:00 | 10-09 08:00 | -0.139543 | negative | 4 | -0.139543 |
| baseline | 10-09 08:00 | 10-09 09:00 | 0.205298 | positive | 2 | 0.205298 |
| baseline | 10-09 09:00 | 10-09 10:00 | 0.162841 | positive | 2 | 0.241484 |
| baseline | 10-09 10:00 | 10-09 11:00 | -0.025165 | negative | 3 | 0.002744 |
| baseline | 10-09 11:00 | 10-09 12:00 | -0.354927 | negative | 1 | -0.277349 |
| cost-stress | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| cost-stress | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| cost-stress | 10-09 02:00 | 10-09 03:00 | 0.000000 | flat | 0 | 0.000000 |
| cost-stress | 10-09 03:00 | 10-09 04:00 | -0.001958 | negative | 0 | 0.000000 |
| cost-stress | 10-09 04:00 | 10-09 05:00 | -0.355324 | negative | 5 | -0.357282 |
| cost-stress | 10-09 05:00 | 10-09 06:00 | 0.127484 | positive | 2 | 0.127484 |
| cost-stress | 10-09 06:00 | 10-09 07:00 | -0.674513 | negative | 4 | -0.674513 |
| cost-stress | 10-09 07:00 | 10-09 08:00 | 0.208853 | positive | 2 | 0.253030 |
| cost-stress | 10-09 08:00 | 10-09 09:00 | -0.982652 | negative | 2 | -0.587019 |
| cost-stress | 10-09 09:00 | 10-09 10:00 | -0.147209 | negative | 2 | -0.587019 |
| cost-stress | 10-09 10:00 | 10-09 11:00 | 0.000000 | flat | 0 | 0.000000 |
| cost-stress | 10-09 11:00 | 10-09 12:00 | 0.000000 | flat | 0 | 0.000000 |
| expanded | 10-09 00:00 | 10-09 01:00 | 0.172620 | positive | 4 | 0.172620 |
| expanded | 10-09 01:00 | 10-09 02:00 | 0.060430 | positive | 3 | 0.239215 |
| expanded | 10-09 02:00 | 10-09 03:00 | -0.023233 | negative | 3 | -0.202018 |
| expanded | 10-09 03:00 | 10-09 04:00 | 0.004417 | positive | 4 | -0.011139 |
| expanded | 10-09 04:00 | 10-09 05:00 | -0.362035 | negative | 6 | -0.165115 |
| expanded | 10-09 05:00 | 10-09 06:00 | 0.634473 | positive | 9 | 0.513102 |
| expanded | 10-09 06:00 | 10-09 07:00 | -1.320053 | negative | 9 | -1.339663 |
| expanded | 10-09 07:00 | 10-09 08:00 | 0.574454 | positive | 9 | 0.543234 |
| expanded | 10-09 08:00 | 10-09 09:00 | -0.581268 | negative | 4 | -0.349400 |
| expanded | 10-09 09:00 | 10-09 10:00 | -0.420666 | negative | 5 | -0.583057 |
| expanded | 10-09 10:00 | 10-09 11:00 | -0.001936 | negative | 4 | 0.025974 |
| expanded | 10-09 11:00 | 10-09 12:00 | -0.354927 | negative | 1 | -0.277349 |
| maker-hypothesis | 10-09 00:00 | 10-09 01:00 | 0.202620 | positive | 4 | 0.202620 |
| maker-hypothesis | 10-09 01:00 | 10-09 02:00 | 0.136049 | positive | 4 | 0.299835 |
| maker-hypothesis | 10-09 02:00 | 10-09 03:00 | -0.015733 | negative | 3 | -0.179518 |
| maker-hypothesis | 10-09 03:00 | 10-09 04:00 | 0.064972 | positive | 4 | 0.018861 |
| maker-hypothesis | 10-09 04:00 | 10-09 05:00 | -0.313952 | negative | 7 | -0.101476 |
| maker-hypothesis | 10-09 05:00 | 10-09 06:00 | 0.717934 | positive | 9 | 0.596563 |
| maker-hypothesis | 10-09 06:00 | 10-09 07:00 | -1.045632 | negative | 9 | -1.057741 |
| maker-hypothesis | 10-09 07:00 | 10-09 08:00 | 0.678094 | positive | 10 | 0.639375 |
| maker-hypothesis | 10-09 08:00 | 10-09 09:00 | -0.813617 | negative | 5 | -0.589249 |
| maker-hypothesis | 10-09 09:00 | 10-09 10:00 | -0.398166 | negative | 5 | -0.545557 |
| maker-hypothesis | 10-09 10:00 | 10-09 11:00 | 0.028064 | positive | 4 | 0.055974 |
| maker-hypothesis | 10-09 11:00 | 10-09 12:00 | -0.347427 | negative | 1 | -0.269849 |
| smaller-book | 10-09 00:00 | 10-09 01:00 | 0.086310 | positive | 4 | 0.086310 |
| smaller-book | 10-09 01:00 | 10-09 02:00 | 0.030215 | positive | 3 | 0.119608 |
| smaller-book | 10-09 02:00 | 10-09 03:00 | -0.011616 | negative | 3 | -0.101009 |
| smaller-book | 10-09 03:00 | 10-09 04:00 | 0.002208 | positive | 4 | -0.005570 |
| smaller-book | 10-09 04:00 | 10-09 05:00 | -0.092891 | negative | 1 | 0.005569 |
| smaller-book | 10-09 05:00 | 10-09 06:00 | 0.200684 | positive | 6 | 0.139999 |
| smaller-book | 10-09 06:00 | 10-09 07:00 | -0.473661 | negative | 6 | -0.503658 |
| smaller-book | 10-09 07:00 | 10-09 08:00 | 0.129684 | positive | 5 | 0.134266 |
| smaller-book | 10-09 08:00 | 10-09 09:00 | -0.208039 | negative | 3 | -0.193344 |
| smaller-book | 10-09 09:00 | 10-09 10:00 | -0.133577 | negative | 4 | -0.152854 |
| smaller-book | 10-09 10:00 | 10-09 11:00 | -0.028725 | negative | 1 | 0.024551 |
| smaller-book | 10-09 11:00 | 10-09 12:00 | -0.177464 | negative | 1 | -0.138674 |

## Limits and release gate

Nine families are implemented: EMA cross/trend, MACD cross, RSI reversal/momentum, Donchian, Bollinger bounce, CCI reversal and OBV trend. This is not the full CTS-A-O catalog, MFE fitting, timeframe-distance scaling, DCA/-Active Signals or the ordinary runtime's coordinator adapter. Model orders/positions are 1:1, not exchange stacking or partial execution. Separate trader fixtures cover existing production paths; venue acceptance remains untested.

Independent reconstruction covers normal, trailing and trailing-only paths; consult the separate audit for each scenario's exact comparison count. Do not merge into a live release until frontend build/render checks, complete Signal integration, forward/out-of-sample evidence and x02 order/fill/protection reconciliation pass. Positive PF and hourly profits cannot be enforced by changing reported results.
