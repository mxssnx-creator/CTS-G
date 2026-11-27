# Axis, Block and DCA on fixed-stop Exec Sets

## Shipped settings

| Setting | Default | Behavior |
| --- | --- | --- |
| Axis Sets | Enabled, last 15, minimum 8, volume 0.75–1.00× | Sizes an already qualified Exec entry from its own Real parent. The selected range can reduce exposure. |
| Block Set adds | Off | Collects per-parent paper observations. A user can enable funded adds separately. Existing direct Block controls remain available. |
| DCA on Sets and on Axis | Off | When enabled, the fixed-stop auto plan uses one rung at 60% of the initial SL distance, 10% of base quantity, and a favorable change between two completed one-minute candles. |
| Maximum SL / TP | 2.5 | Filters fixed-stop candidates against each configuration’s target and preserves both band identities in the overlap. |

## Replay result

The 32 candidates used identical historical Exec parents from the confluence and RSI study preset. All entered from qualified Base → Main → Real → Exec paths. A 0.8% stop, 0.8% target, 1.50 admission PF and 0.11% round-trip modeled cost applied. Every add paid its own cost. Sizing and add gates only saw evidence available at their decision time. The cost, order count and PF shown here describe simulation.

| November 2026 UTC | Symbols | Settled parents | Still open | Neutral PF | Selected Axis PF | Neutral drawdown | Selected Axis drawdown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 11–13 | 30 | 100 | 0 | 1.1237 | 1.0846 | 22.70 | 21.56 |
| 14–16 | 30 | 259 | 0 | 1.3492 | 1.3949 | 28.37 | 23.66 |
| 17–19 | 12 | 21 | 0 | 0.2370 | 0.2370 | 12.27 | 12.27 |
| 19–21 | 30 | 99 | 0 | 0.4929 | 0.4479 | 37.97 | 35.96 |
| 21–23 | 32 | 184 | 0 | 2.2465 | 2.2221 | 15.47 | 15.47 |
| 24–26 | 32 | 169 | 5 | 1.4533 | 1.3917 | 16.38 | 16.38 |
| 26–27 | 32 | 44 | 2 | 0.5074 | 0.5434 | 16.52 | 13.23 |

Drawdown is the largest peak-to-trough change in realized net P&L over one window, expressed as a percentage of one parent’s starting notional. Positions that closed together contributed one balance observation. Account leverage, intrabar mark-to-market drawdown and funding payments are outside this measure. The seven open positions are excluded from realized PF. Gross positive and gross negative net-of-cost outcomes are pooled across windows to compute the aggregate PF.

November 11–13 and 19–21 were reserved from parameter selection. Both had lower drawdown with the selected Axis settings. Their PF fell from 1.1237 to 1.0846 and from 0.4929 to 0.4479 respectively. On the five selection windows, pooled PF moved from 1.3813 to 1.4019 and worst drawdown from 28.37 to 23.66. Three of the seven Axis windows had PF below one.

| Configuration with 876 settled parents | Pooled Exec PF | Worst window drawdown | Simulated adds |
| --- | ---: | ---: | ---: |
| Neutral sizing | 1.1937 | 37.97 | 0 |
| Axis 8 closes, 0.50–1.00× | 1.1780 | 34.78 | 0 |
| Axis 15 closes, 0.75–1.00× | 1.1939 | 35.96 | 0 |
| Axis 25 closes, 0.50–1.50× | 1.1965 | 50.67 | 0 |
| Block, two cost steps, 0.10× | 1.1913 | 37.97 | 33 |
| Selected Axis plus Block | 1.1917 | 35.96 | 33 |
| Legacy DCA, 0.40 of SL, 0.50× | 1.1743 | 40.13 | 311 |
| Opt-in DCA, 0.60 of SL, 0.10×, bounce | 1.1920 | 37.97 | 46 |
| Selected Axis plus opt-in DCA | 1.1921 | 35.96 | 46 |

The grid covered four Axis settings, the shipped Block policy and six alternatives, 18 DCA settings, two combinations and a neutral control. The most aggressive Axis candidate had a slightly higher pooled PF and materially greater drawdown. All six actively funding Block alternatives lowered pooled PF. The conservative DCA plan reduced risk relative to the earlier DCA settings. Enabling DCA together with Axis still lowered pooled PF relative to Axis alone. The study does not establish a universal winning strategy.

## Additional replay after selection

The frozen settings were also replayed on November 9–11 UTC, covering 30 symbols, 60 settled parents and zero open positions at the cutoff. Neutral PF was 0.2527 with realized drawdown 36.128. Axis PF was 0.2555 with drawdown 35.673. Across the two originally reserved windows, neutral PF was 0.7392 and Axis PF was 0.6959 over 199 closes.

| Configuration over all eight windows | Closed | Pooled Exec PF | Worst realized drawdown | Simulated adds |
| --- | ---: | ---: | ---: | ---: |
| Neutral | 936 | 1.0828 | 37.97 | 0 |
| Selected Axis | 936 | 1.0770 | 35.96 | 0 |
| Axis plus Block | 936 | 1.0754 | 35.96 | 33 |
| Axis plus DCA | 936 | 1.0756 | 35.96 | 49 |

Four of eight Axis windows lost money. The selected size range reduced the worst drawdown and slightly reduced pooled PF and held-out PF. Its evidence supports using it to limit exposure. Independent profitability is unproven.

## Calculation cost

On a 60-close fixture with combined Axis rules enabled, the previous implementation took 14.77 ms per evaluation and the updated implementation took 1.75 ms. Both selected the same rule, volume and admission decision. These are medians of five batches of 20 calls on this machine. The updated kernel evaluates each distinct rule once per history prefix. DCA reuses the score for an unchanged owner and invalidates it on a confirmed close or a policy change.

Across 10,000 newly constructed empty Sets, Python 3.12 allocated an average of 1,457 bytes per Set, down from 3,856 bytes. This measures Python allocations with shared geometry strings before market history arrives. Each Set still owns its mutable histories and reads older dictionary-based replay snapshots.

## Execution and reproduction

Each Set add rechecks its owning Exec path. Block and DCA persist the client order identity before submission and use the venue’s confirmed cumulative fills, including partial quantities and fees. Repeated receipts and restarts preserve each leg’s quantity. The replay uses minute-close fills without a venue queue or lot-size simulation. The native settlement tests separately exercise acknowledgements, partial fills, restarts, foreign receipts and unchanged parent economics.

Use an archived candle directory and the local matching simulator cache and receipts. The comparison verifies the candle hashes against the original run.

At startup, DCA rebuilds each owner's evaluation window from eligible journal records, including trades older than the 80-row activity display. The bounce condition compares consecutive completed minutes for the same symbol. An intraminute tick or a gap cannot qualify a rung.

```sh
python scripts/trade_sim.py --data "$CANDLE_DIR" --symbols 32 --last-hours 48 \
  --patch presets/fixed-stop-confluence-rsi-stage-study.json --refresh 0 --eval-every 3600 \
  --stage-details --jobs 2 --cache "$CACHE_PATH" --execution-cache "$RECEIPTS_PATH" --out "$BASE_REPORT"
PYTHONPATH=server/pulse:scripts python scripts/fixed_parent_coordination_study.py \
  --market "$CANDLE_DIR" --symbols 32 --cache "$CACHE_PATH" --receipts "$RECEIPTS_PATH" --out "$COMPARISON"
python scripts/test_position_settlement.py
```
