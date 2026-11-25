# Signals sources, 12 symbols, 5-day window (2026-11-25)

Owner: "remove non performing Sources ... optimize for highest performance". Measured with `scripts/trade_sim.py`: BingX 1m data, 12 symbols
(BTC ETH SOL XRP BNB DOGE ADA AVAX LINK DOT BCH LTC), 3 days pre-history (`histLookbackBars` 4320, `histLookbackAuto` false) and a 120 h report window
(`--last-hours 120`). This is above the 3-day limit in CLAUDE.md and was run on the owner's explicit request.

Signals only: every other pack off (`stratIndications`, `stratGeneral`, `stratShortRange`, trend / breakout / reversion / flow / multi false), one `signalConfigs`
entry with all 9 sources x 2 ranges x timeframes 1 / 5 / 15 / 30, TP steps 5-12, SL 0.5 / 0.75 / 1.0, base + trailing: 72 packs, 4,752 Sets.
Run time 855 s, parent peak RSS 1.1 GiB, worker peak 332 MiB (per-symbol checkpoints: `STAGE_SYMBOL_CHECKPOINT_DIR`).

Baseline (all 72 packs): executed PF 0.829 (closed and marked), 864 orders; exits tp 313 / sl 518 / time 33.
"dev" = first 80 h of the window, "late" = last 40 h. Not a sealed window: the whole window was read once, so no source below was tuned on a held-out part.

| source | dev PF / orders / net % | late PF / orders / net % | all PF / orders / net % |
|---|---|---|---|
| macd-cross | 0.64 / 173 / -27.35 | 1.41 / 113 / +16.19 | 0.90 / 286 / -11.17 |
| cci-reversal | 0.92 / 131 / -4.73 | 0.61 / 83 / -15.35 | 0.79 / 214 / -20.08 |
| bb-bounce | 0.66 / 74 / -12.00 | 1.18 / 34 / +2.14 | 0.79 / 108 / -9.86 |
| donchian | 0.82 / 45 / -2.98 | 0.33 / 16 / -6.91 | 0.64 / 61 / -9.89 |
| obv-trend | 1.00 / 44 / +0.02 | 0.86 / 15 / -0.85 | 0.96 / 59 / -0.83 |
| ema-trend | 1.45 / 34 / +6.17 | 0.83 / 23 / -1.82 | 1.18 / 57 / +4.35 |
| ema-cross | 0.76 / 25 / -2.92 | 1.22 / 14 / +1.18 | 0.90 / 39 / -1.74 |
| rsi-reversal | 0.26 / 18 / -9.82 | 0.46 / 15 / -3.68 | 0.33 / 33 / -13.50 |
| rsi-momentum | 0.08 / 7 / -5.73 | no orders | 0.08 / 7 / -5.73 |

By timeframe (all sources): 1 min 0.79 / 701 / -64.48; 5 min 1.07 / 102 / +2.90; 15 min 0.75 / 52 / -5.40; 30 min 0.57 / 9 / -1.47.
By range: short 0.84 / 638 / -44.36; medium 0.77 / 226 / -24.08. By kind: base 0.82 / 675, trailing 0.80 / 189.

## Decision

Rule (CLAUDE.md 2026-11-15): better than the current setting on both parts, then prefer more orders.
**Removed from the shipped `signalConfigs`:** rsi-momentum, rsi-reversal, donchian, cci-reversal, obv-trend (under the baseline on at least the late part and
at or under PF 1 on both; together 374 of 864 orders, net -50.0 %). **Kept:** macd-cross, bb-bounce, ema-cross, ema-trend.
Applied through `sources` in both overlays and the desk default (`SHIPPED_SIGNAL_CONFIGS`); the code of the removed sources stays, revert = drop the `sources` key.

The four kept sources are each above 1 on one part only (the sign flips between dev and late), 39-286 orders: this removes the clear losers, it does not show an edge.
Untested here: new sources from web research (none added; a source needs the dev-and-late rule above before it ships), and the 1 min timeframe,
which carries 701 of 864 orders at PF 0.79 (left unchanged, it is the owner's setting).
