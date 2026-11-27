# Fixed stops, entry capacity and Exec performance

The engine measures the initial stop and target in percentage points of the entry price. Each selected stop must fit the target band and the inclusive maximum SL / TP ratio. The default ratio is 2.5. The engine skips an ineligible pair before it allocates a Set. It never shortens the stop to manufacture a matching pair.

| Band | Reference TP | Fixed SL choices |
| --- | --- | --- |
| Short | 0.1–1.6% | 0.2–1.6% by 0.1 |
| Long | 1.2–5.0% | 0.4–2.6% by 0.2 |

Both bands run in the overlap and retain separate identities, trade directions and stage receipts. An Exit Set uses its configured target step as the reference. A trailing Set with a target uses that target. A targetless trailing Set uses its activation distance. The selected fee, minimum stop, range and indication gates also apply. For example, a 0.4% reference TP at 2.5 accepts 1.0% SL and excludes 1.1%.

## Frozen market replays

The research preset uses two reversal indications on both sides, a fixed 0.8% target and stop, and a 1.50 admission PF. The replay charged 0.11% round trip and settled only closed positions. The late 24-hour check and the 12-symbol check both lost money. The PF threshold therefore remains an opt-in study setting.

| UTC window in November 2026 | Symbols | Closed | Open at end | Exec PF | Net, % of one base notional |
| --- | ---: | ---: | ---: | ---: | ---: |
| 14–16 | 30 | 259 | 0 | 1.349 | +29.56 |
| 17–19 | 12 | 21 | 0 | 0.237 | −11.11 |
| 21–23, development | 32 | 184 | 0 | 2.247 | +52.30 |
| 24–26, subsequent | 32 | 169 | 5 | 1.453 | +23.67 |
| 26–27, subsequent | 32 | 44 | 2 | 0.507 | −11.66 |

With one confluence indication, a 1.2% target, both sides and the full band selections, 46 root Sets survived the configured stop floor. The 12-symbol November 21–23 and November 24–26 replays closed four and six orders respectively. Both had PF 0. A separate November 26–27 12-symbol check at a 0.4% target and a selected stop list of 0.4, 0.8, 1.0, 1.2 and 1.6% produced two, six and ten roots at ratio ceilings of 1, 2.5 and 5. The first two closed zero orders. The 5 ceiling closed eight at PF 0.231.

On the 32-symbol development interval, a 12-slot cap at the 1.50 PF threshold reduced 184 orders to 144 and moved PF from 2.247 to 2.239. The per-group and total open limits now use the same exposure definition in replay and the trader. The trader reserves unresolved owned entries before counting a slot again.

```sh
python scripts/test_stage_grid.py
python scripts/trade_sim.py --data "$CANDLE_DIR" --symbols 32 --last-hours 48 \
  --patch presets/fixed-stop-confluence-rsi-stage-study.json --refresh 0 --eval-every 3600 \
  --stage-details --jobs 2 --cache "$CACHE_PATH" --out "$REPORT_PATH"
```
