# Evaluation minimum PF: selectivity versus quality

The shared **Overall minimum PF** (Settings, Profit factor; profile keys `minPf`,
`baseMinPf`, `mainMinPf`, `realMinPf`, `setMinPf`, `dcaMinPf`, `exitMinPf`) gates Base
(last 30), Main (last 5) and Real (last 3). It is set to **1.20**
(`EVAL_MIN_PF` in `server/pulse/connection_profile.py`, seeds in
`server/pulse/overlay-bingx-x0*.json`). Slider range 1.02 to 1.35.

## What was measured

`scripts/min_pf_sweep.py` reruns the walk-forward stage chain of
`scripts/sim_12h_account.py` on the same replay (48 symbols, 24,960 Sets) with every
stage floor set to one value. Evidence is strictly earlier than each entry bar, so each
row is out of sample. "Entries admitted" is the share of Set entry candidates that pass
Base, Main, Real and the drawdown-time gate. PF normal is the classic profit factor
after PositionCost (0.10 %).

### final 12h (bars 9360-10080)

| min PF | entries admitted | trades | PF normal | cost PF | win rate | net per trade |
|---:|---:|---:|---:|---:|---:|---:|
| 1.02 | 39.8 % | 13,913,237 | 0.828 | 0.954 | 67.6 % | -0.0459 % |
| 1.05 | 37.5 % | 13,112,693 | 0.840 | 0.957 | 68.0 % | -0.0426 % |
| 1.10 | 32.1 % | 11,196,799 | 0.862 | 0.963 | 68.6 % | -0.0372 % |
| 1.15 | 25.8 % | 8,945,501 | 0.929 | 0.981 | 69.6 % | -0.0191 % |
| 1.20 | 20.2 % | 6,976,136 | 0.972 | 0.993 | 69.6 % | -0.0075 % |
| 1.25 | 15.2 % | 5,205,009 | 0.999 | 1.000 | 70.3 % | -0.0003 % |
| 1.30 | 10.8 % | 3,618,108 | 1.048 | 1.013 | 72.2 % | +0.0129 % |

### previous 12h (bars 8640-9360)

| min PF | entries admitted | trades | PF normal | cost PF | win rate | net per trade |
|---:|---:|---:|---:|---:|---:|---:|
| 1.02 | 38.0 % | 8,528,665 | 0.926 | 0.983 | 74.8 % | -0.0173 % |
| 1.10 | 29.0 % | 6,512,958 | 0.878 | 0.970 | 74.6 % | -0.0298 % |
| 1.15 | 21.3 % | 4,771,168 | 0.887 | 0.972 | 75.1 % | -0.0276 % |
| 1.20 | 15.0 % | 3,363,159 | 0.883 | 0.973 | 75.5 % | -0.0273 % |
| 1.25 | 8.8 % | 1,971,091 | 0.811 | 0.954 | 73.0 % | -0.0457 % |
| 1.30 | 6.0 % | 1,346,759 | 0.706 | 0.928 | 71.9 % | -0.0723 % |
| 1.35 | 4.0 % | 895,729 | 0.705 | 0.928 | 72.4 % | -0.0718 % |

Mean PF normal over the two windows:

| min PF | PF normal, final 12h | PF normal, previous 12h | mean |
|---:|---:|---:|---:|
| 1.02 | 0.828 | 0.926 | 0.877 |
| 1.10 | 0.862 | 0.878 | 0.870 |
| 1.15 | 0.929 | 0.887 | 0.908 |
| 1.20 | 0.972 | 0.883 | 0.927 |
| 1.25 | 0.999 | 0.811 | 0.905 |
| 1.30 | 1.048 | 0.706 | 0.877 |

## What it means

- **Selectivity works.** Raising the floor cuts the admitted entries steadily:
  about 40 % at 1.02, 26 % at 1.15, 20 % at 1.20, 11 % at 1.30 (final window).
- **Quality does not follow reliably.** In the final window PF normal rises with the floor
  (0.83 to 1.05). In the previous window it falls (0.93 to 0.71). Two windows disagree, so a
  higher floor is a selectivity setting, not a guarantee of profitable Sets. Only one row of
  the whole table is above PF 1.0, and it sits in one window.
- **Why 1.20.** It admits roughly 15 to 20 % of the entries, has the best mean of the
  measured values, and avoids the extremes (1.30 and above lost the most in the previous
  window while trading a small sample).
- **Sets versus entries.** Almost every Set passes at some entry moment (100 % of the
  Sets at 1.02 to 1.20 in the final window), because the gate is evaluated at every entry bar.
  The floor thins entries, not the catalog.
- **Limits.** Two comparable 12h windows only (the replay cache holds trades for the final
  window and the one before it). Choosing the best floor on the same windows would overfit,
  so treat the mean as a tie-breaker and rerun `min_pf_sweep.py` on fresh data.

## Applying it on a running server

Deploying the code does not change a lane that already has its own overlay
(`update-linux.sh` and the rollout keep server edits). Set it on each lane in the desk
(Settings, Profit factor, Overall minimum PF, then Save Live and VST) or through the sidecar:

```bash
for lane in live vst; do
  curl -s -X POST "http://127.0.0.1:3015/config.json?conn=$lane" \
    -H 'Content-Type: application/json' -d '{"overlay":{"minPf":1.20}}'
done
```

A `minPf` write sets all seven stage floors together. The engine reloads the overlay on its
next cycle; no restart is needed.

## Full 12h account simulation at 1.20

`reports/sim-12h-account.json/.html` were regenerated with the new floor (10 USD start,
hourly table, same cached replay). Trade level, final 12h window:

| Stage | Trades | Classic PF | Cost PF | Win rate | Net avg / trade |
|---|---|---|---|---|---|
| Unfiltered | 35.1 M | 0.662 | 0.900 | 63.6 % | -0.100 % |
| Base passed (last-30 ≥ 1.20) | 10.3 M | 0.922 | 0.979 | 67.9 % | -0.021 % |
| Base + Main + Real | 7.0 M | 0.972 | 0.993 | 69.6 % | -0.008 % |

The same gate at 1.02 (the previous deployed profile) admitted 14.4 M entry candidates
(13.9 M trades, PF 0.83, net avg -0.046 %); at 1.20 it admits 7.3 M (PF 0.97). The chain is
now close to break-even after cost, not above it. No floor in the 1.02 to 1.35 range turned
the whole book profitable on this data.

## Where the value lives

- `EVAL_MIN_PF` in `server/pulse/connection_profile.py`: processing profile pushed by
  `prepare_connection_profile.py` (was 1.02) and the seed for new lanes.
- `server/pulse/overlay-bingx-x01.json`, `overlay-bingx-x02.json`: repo seeds (were 1.15).
- `EVAL_MIN_PF` in `src/lib/config-model.ts`: desk default, so a desk save does not put the
  floor back. `scripts/test_connection_profile.py` fails if the two constants drift apart.
- `POSITIVE_PF` (1.15) and `histTestMinPf` (1.15) stay as they are: the "positive Set" label
  and the Test Historic universe floor are separate settings.
