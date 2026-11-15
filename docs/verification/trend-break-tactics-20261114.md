# Trend and break tactics, barrier test (2026-11-14)

New families `trendpb`, `trendflag`, `brkvol`, `brkretest`, `rangebrk` (groups `microtrend` / `microbreak`, outside every voted group) next to the nine earlier micro tactics.

Test: 40 liquid USDT perpetuals, 3 days of 1m bars (4,320 each, the three-day limit), a signal every 4th bar from bar 500, 84 names (14 families x ranges 5 / 8 / 14 x 1m / 5m).
Each signal entered at the close with TP step 2 / 3 / 5 (0.2 / 0.3 / 0.5 %, the Micro and Short ranges), SL ratio 0.5 / 0.75 / 1.0, 60-bar time exit, stop first, 0.11 % cost per order.
Development = first 66 % of the bars, sealed = the rest. The test uses the plain planned stop (no minimum-stop floor), so it understates what the engine's floored stops do.

Pooled PF (development | sealed), step 2 / 3 / 5: every family reads 0.18 - 0.31 / 0.30 - 0.47 / 0.41 - 0.66 (dev) and 0.18 - 0.38 / 0.32 - 0.47 / 0.49 - 0.66 (sealed).
**0 of 756 (name, step, ratio) cells have PF above 1 on both windows** (at least 300 development and 150 sealed signals). None is added to a pack or to the live grid.
The new families are no better and no worse than the older micro tactics. Raw data: scratchpad `tacbar/*.json` (not kept).
