# Signals retrospective diagnostic — 1 November 2026

Offline experiment only. No x02/demo orders, production integration or live acceptance. No configuration is promoted.

Exactly eight archived symbols, 12 hours prehistory and 12 hours modeled trading. No hidden earlier warmup. Window 8 October 2026 12:00–9 October 2026 12:00 UTC (14:00–14:00 Europe/Vienna); the second half is trading. The archive has been studied before: this is **not an untouched holdout**.

Per-leg fees use each leg's own notional, with adverse entry/exit slippage. Configured PF floors: [2.2]. A maker scenario is an assumed fill, not proof of a maker execution. Fractions and source identities are frozen in adjacent `.config.json` files. A `-frozen` case admits only exact contexts qualifying before trading; dynamic cases can qualify later. All ongoing gates remain active.

## Modeled execution, including losses

| Scenario | Contexts | Settled / still open | Closed-return PF | Equity change | Mark drawdown % | Positive / negative / flat hours | Seconds |
|---|---:|---:|---:|---:|---:|---:|---:|
| existing-six | 4608 | 45 / 1 | 0.812145 | -0.921648 | 0.250699 | 4 / 6 / 2 | 2.907 |
| new-three-frozen | 4608 | 0 / 0 | empty | 0.000000 | 0.000000 | 0 / 0 / 12 | 3.159 |
| nine-dynamic | 6912 | 49 / 1 | 0.795153 | -1.177300 | 0.261282 | 4 / 6 / 2 | 4.914 |
| nine-frozen | 6912 | 0 / 0 | empty | 0.000000 | 0.000000 | 0 / 0 / 12 | 4.608 |
| nine-stress | 6912 | 14 / 0 | 0.222037 | -2.055056 | 0.205506 | 2 / 4 / 6 | 4.593 |
| wide-dynamic | 11520 | 12 / 3 | 0.739579 | -0.605238 | 0.179620 | 2 / 3 / 7 | 7.669 |
| wide-frozen | 11520 | 0 / 0 | empty | 0.000000 | 0.000000 | 0 / 0 / 12 | 7.609 |

Initial modeled equity is 1,000 quote units. Marks include estimated close fees/slippage on open positions; no forced end-of-test close hides the remaining exposure. No funding, liquidation, intraminute liquidity or venue netting is modeled. Low drawdown here reflects small modeled exposure and this short window, not a reliable future bound.

Net-positive ending equity: 0/7 scenarios. Positive every hour: 0/7. Empty/flat hours are not wins. Passing historical windows are not a portfolio profit guarantee.

## wide-dynamic: final stage evidence

Each exact source/range/TF/symbol/direction/protection/Last-N branch owns separate Base, Main and Real positions. Main and Real re-simulate their admitted entries; they are not filtered copies of Base outcomes. Historical current-window qualification is not future execution profitability.

| Stage | Own passing / population | % passing | Passing through this stage | Insufficient own Last-N | Passing-window PF | All modeled closes | All-close PF |
|---|---:|---:|---:|---:|---:|---:|---:|
| base | 246 / 11520 | 2.135417 | 246 | 9832 | 3.900905 | 49372 | 1.019698 |
| main | 50 / 11520 | 0.434028 | 44 | 11301 | 3.765669 | 3129 | 1.213822 |
| real | 9 / 11520 | 0.078125 | 9 | 11497 | 3.096087 | 324 | 0.913712 |

Stage totals pool hypothetical overlapping branches and shared price events for diagnosis only; they are not a portfolio PF or independent statistical observations.

Source coverage: 88/576 source/symbol/range/TF combinations lack minimum indicator warmup at trading start; 32 still lack it at the end. Warm indicators can still lack enough own stage closes. The engine reports this insufficiency instead of fabricating prehistory, pooling another lane, or lowering the PF floor.

Detailed grid lines are in `signals-wide-stage-grid-20261101.md`; the compressed scenario files retain every exact symbol/direction context, hourly stage progress, every modeled execution and all 721 close equity marks plus opening/admission marks.

## Every modeled trading hour (UTC)

Equity changes include open-position marks. Settled cash alone is not hourly equity profit. Exact boundary convention: candle closes settle in `(from, to]`.

| Scenario | From | To | Equity change | State | Settled orders | Settled cash |
|---|---|---|---:|---|---:|---:|
| existing-six | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| existing-six | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| existing-six | 10-09 02:00 | 10-09 03:00 | 0.102830 | positive | 1 | 0.102830 |
| existing-six | 10-09 03:00 | 10-09 04:00 | 0.141284 | positive | 3 | 0.141284 |
| existing-six | 10-09 04:00 | 10-09 05:00 | -0.074151 | negative | 1 | 0.107214 |
| existing-six | 10-09 05:00 | 10-09 06:00 | 0.651999 | positive | 8 | 0.681487 |
| existing-six | 10-09 06:00 | 10-09 07:00 | -1.071305 | negative | 10 | -1.282158 |
| existing-six | 10-09 07:00 | 10-09 08:00 | 0.686493 | positive | 8 | 0.695658 |
| existing-six | 10-09 08:00 | 10-09 09:00 | -0.581268 | negative | 4 | -0.349400 |
| existing-six | 10-09 09:00 | 10-09 10:00 | -0.420666 | negative | 5 | -0.583057 |
| existing-six | 10-09 10:00 | 10-09 11:00 | -0.001936 | negative | 4 | 0.025974 |
| existing-six | 10-09 11:00 | 10-09 12:00 | -0.354927 | negative | 1 | -0.277349 |
| new-three-frozen | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 02:00 | 10-09 03:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 03:00 | 10-09 04:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 04:00 | 10-09 05:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 05:00 | 10-09 06:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 06:00 | 10-09 07:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 07:00 | 10-09 08:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 08:00 | 10-09 09:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 09:00 | 10-09 10:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 10:00 | 10-09 11:00 | 0.000000 | flat | 0 | 0.000000 |
| new-three-frozen | 10-09 11:00 | 10-09 12:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-dynamic | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-dynamic | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-dynamic | 10-09 02:00 | 10-09 03:00 | 0.102830 | positive | 1 | 0.102830 |
| nine-dynamic | 10-09 03:00 | 10-09 04:00 | 0.287912 | positive | 3 | 0.141284 |
| nine-dynamic | 10-09 04:00 | 10-09 05:00 | -0.390380 | negative | 2 | -0.054998 |
| nine-dynamic | 10-09 05:00 | 10-09 06:00 | 0.756256 | positive | 8 | 0.848131 |
| nine-dynamic | 10-09 06:00 | 10-09 07:00 | -0.788688 | negative | 8 | -1.227161 |
| nine-dynamic | 10-09 07:00 | 10-09 08:00 | 0.881730 | positive | 11 | 1.048736 |
| nine-dynamic | 10-09 08:00 | 10-09 09:00 | -0.858617 | negative | 5 | -0.626749 |
| nine-dynamic | 10-09 09:00 | 10-09 10:00 | -0.374904 | negative | 5 | -0.583057 |
| nine-dynamic | 10-09 10:00 | 10-09 11:00 | -0.438510 | negative | 5 | -0.364838 |
| nine-dynamic | 10-09 11:00 | 10-09 12:00 | -0.354927 | negative | 1 | -0.277349 |
| nine-frozen | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 02:00 | 10-09 03:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 03:00 | 10-09 04:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 04:00 | 10-09 05:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 05:00 | 10-09 06:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 06:00 | 10-09 07:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 07:00 | 10-09 08:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 08:00 | 10-09 09:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 09:00 | 10-09 10:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 10:00 | 10-09 11:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-frozen | 10-09 11:00 | 10-09 12:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-stress | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-stress | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-stress | 10-09 02:00 | 10-09 03:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-stress | 10-09 03:00 | 10-09 04:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-stress | 10-09 04:00 | 10-09 05:00 | -0.587019 | negative | 2 | -0.587019 |
| nine-stress | 10-09 05:00 | 10-09 06:00 | 0.127484 | positive | 2 | 0.127484 |
| nine-stress | 10-09 06:00 | 10-09 07:00 | -0.674513 | negative | 4 | -0.674513 |
| nine-stress | 10-09 07:00 | 10-09 08:00 | 0.208853 | positive | 2 | 0.253030 |
| nine-stress | 10-09 08:00 | 10-09 09:00 | -0.982652 | negative | 2 | -0.587019 |
| nine-stress | 10-09 09:00 | 10-09 10:00 | -0.147209 | negative | 2 | -0.587019 |
| nine-stress | 10-09 10:00 | 10-09 11:00 | 0.000000 | flat | 0 | 0.000000 |
| nine-stress | 10-09 11:00 | 10-09 12:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 02:00 | 10-09 03:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 03:00 | 10-09 04:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 04:00 | 10-09 05:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 05:00 | 10-09 06:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 06:00 | 10-09 07:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-dynamic | 10-09 07:00 | 10-09 08:00 | 0.074557 | positive | 3 | 0.074557 |
| wide-dynamic | 10-09 08:00 | 10-09 09:00 | 0.312242 | positive | 0 | 0.000000 |
| wide-dynamic | 10-09 09:00 | 10-09 10:00 | -0.262663 | negative | 2 | -0.009563 |
| wide-dynamic | 10-09 10:00 | 10-09 11:00 | -0.165254 | negative | 2 | -0.222071 |
| wide-dynamic | 10-09 11:00 | 10-09 12:00 | -0.564120 | negative | 5 | -0.280745 |
| wide-frozen | 10-09 00:00 | 10-09 01:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 01:00 | 10-09 02:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 02:00 | 10-09 03:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 03:00 | 10-09 04:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 04:00 | 10-09 05:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 05:00 | 10-09 06:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 06:00 | 10-09 07:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 07:00 | 10-09 08:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 08:00 | 10-09 09:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 09:00 | 10-09 10:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 10:00 | 10-09 11:00 | 0.000000 | flat | 0 | 0.000000 |
| wide-frozen | 10-09 11:00 | 10-09 12:00 | 0.000000 | flat | 0 | 0.000000 |

## Limits and release gate

Nine families are implemented: EMA cross/trend, MACD cross, RSI reversal/momentum, Donchian, Bollinger bounce, CCI reversal and OBV trend. This is not the full CTS-A-O catalog, MFE fitting, timeframe-distance scaling, DCA/-Active Signals or the ordinary runtime's coordinator adapter. Model orders/positions are 1:1, not exchange stacking or partial execution. Separate trader fixtures cover existing production paths; venue acceptance remains untested.

Independent reconstruction covers normal, trailing and trailing-only paths; consult the separate audit for each scenario's exact comparison count. Do not merge into a live release until frontend build/render checks, complete Signal integration, forward/out-of-sample evidence and x02 order/fill/protection reconciliation pass. Positive PF and hourly profits cannot be enforced by changing reported results.
