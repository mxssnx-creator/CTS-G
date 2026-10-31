# Automatic-hold and forward-stage audit — 31 October 2026

This is a draft, offline investigation. It is not a profitable or live-ready strategy, and it must not be used to start x01. The exchange connection, current positions, owned orders, protection and actual fees were not verified.

## Defect and correction

The prior automatic-hold replay spliced trades from separately simulated hold candidates. Even though each candidate choice used older closes, trades from different candidate tapes could overlap in the same Set and symbol, and the splice could omit entries. Its Base, Main and Real statistics therefore do not establish a realizable position sequence. The earlier preset and expanded-pack results have been withdrawn in their report and archived data.

The draft now chooses a hold at each paper entry and keeps that hold for the life of that position. It replays one lane per Set and symbol, including cooldown, instead of splicing rows. Candidate evidence is retained across the available entry history and is restricted to the rolling lookback at each choice. If paired replay inputs are unavailable, the Set fails scoring rather than falling back to the splice. This changes historical paper evidence; it does not reconcile a live order book.

An independent bar-by-bar walk matched the indexed replay for base and trailing Sets with several hold changes, and chunked replay preserved the held position and cooldown. A synthetic fixture produced five overlaps in the old splice and zero in the new lane, while 27 corrected rows were absent from the splice. Tests also cover same-clock exclusion, expiry, later-bar independence, more than 50 candidate closes, and the Signals floor on separate Main and Real windows. The CTS-A-O Signal-source engine itself is not connected in CTS-G.

In a separate [archived-data prefix check](secure-minimal-hold-prefix-20261031.json), removing later candles did not change the 110 prior closes of one base Set or the 125 prior closes of one trailing Set across ADA and AVAX. This is a two-Set, two-symbol causality check, not a live parity test.

The updated **Secure, Minimal** preset disables the existing Negative lane, which can bypass the positive Main gate and does not implement the requested independent opposite-direction strategy. The systemwide default is unchanged. This is a safety restriction, not completion of that strategy. The saved JSON and Settings preset are tested for equality.

## Corrected checkpoint and subsequent outcomes

All studies use the previously inspected archive of eight symbols and an **assumed** 0.12% round-trip cost. They do not include actual account fees, venue fills or a 50-symbol execution test. The windows below contain overlapping Sets and market observations; they are not independent funded positions.

At the hindsight-selected **17 October, 19:00 Vienna time** (17:00 UTC), a fresh 12-hour cold-start snapshot of the revised preset scored 67 of 822 Sets through Base, 58 through Main, and none through Real. The 58 Main-passing Sets held only 5–8 Real-window closes against the required 15. A separate stage-chain calculation matched all 67 Base-passing Sets on the same pooled paper tapes. This checks that calculation, not the validity of its source identities or missing stage context. [Snapshot and hashes](secure-minimal-current-checkpoint-20261031.json).

A different **continuous** paper study reconstructed positions through the entire archived period, used strictly earlier closes at each hourly decision, and examined subsequent entries in the same Set. Its initial state, history and clock differ from the cold-start snapshot; the two sets of counts should not be compared as if they were identical runs. Across 12 checkpoints, eight Set-checkpoint observations passed Base; none passed Main or Real. All 210,541 reconstructed closes were checked for same-Set/symbol overlap, with none found. An order-by-order stage oracle matched the eight Base observations using the same bounded input.

| Following paper window | Mature Set-checkpoint observations | Net returns counted | Pooled PF |
| --- | ---: | ---: | ---: |
| Next 8 entries | 7 | 56 | 0.258 |
| Next 15 entries | 4 | 60 | 0.561 |

Only windows with all requested subsequent closes before the archive ended are included. The next-8 and next-15 cohorts differ. Results include losses and subtract the assumed cost once. They are retrospective, selected Set tapes, not executed trades or an unseen holdout. The underlying observations and input/source hashes are in [the forward data](secure-minimal-forward-after-base-20261031.json). A separate arithmetic recount reproduced the gains, losses and PFs from those records.

The same study was repeated with **72 hours of available prehistory** and the same 12-hour trading period. It found 25 Base Set-checkpoint observations across 21 distinct Sets, with no Main or Real observation passing. The 25 complete next-eight windows contained 200 returns and had pooled PF **0.253**; 22 complete next-15 windows contained 330 returns and had pooled PF **0.292**. An arithmetic recount agreed with the stored values. The run checked 1,116,351 reconstructed closes for same-Set/symbol overlap and found none; its stage oracle matched all 25 Base observations on the same bounded tapes. This uses the same retrospective trading period and does not create an independent holdout. [Longer-prehistory data and hashes](secure-minimal-forward-72h-prehistory-20261031.json).

The full corrected cold-start checkpoint series using the **earlier** preset with Negative enabled yielded 120 Base and 93 Main Set-checkpoint observations, and zero Real/Real Negative observations. This is a separate configuration and overlapping snapshot series, not the revised preset's forward result. [Prior-preset corrected series](secure-minimal-causal-hold-checkpoints-20261031.json).

## Remaining release blockers

- A high historical Base PF cannot ensure a high or increasing forward PF. The continuous results above do not meet that goal.
- The current stage reconstruction can lose older evidence needed to decide whether an entry belonged to Main or Real. The separate [stage-history draft](https://github.com/mxssnx-creator/CTS-G/pull/130) documents a proposed repair and unresolved performance and timing checks. The independent oracle here reads the same bounded tapes and does not resolve that defect.
- The study selects holds at every paper entry; live choices refresh periodically. It excludes closes at the decision timestamp, while the runtime scorer currently includes them. The actual refresh/entry/close order needs a shared, tested contract.
- Set evidence remains pooled across symbols and directions; the requested independent source, range and symbol/side qualification and actual CTS-A-O Signals integration remain unfinished. The requested independent inverse Negative strategy is also unfinished.
- Eight archived symbols and modeled costs do not establish 50-symbol capacity, low realized drawdown, live PF, exchange fees, order ownership or protective-order behavior. Existing positions and market gaps can exceed modeled risk limits.

## Reproduction

With the same checked-in CTS-A-O Signals coverage feed available locally, create a new scratch directory for each data preparation. The archived feed SHA-256 is `e21d39b0b7583685e2fe355f6261e0860deff3057ca5c81b43a6c3cf374e2f03`. These commands are paper analysis and do not connect to a venue.

```sh
python3 scripts/prepare_signal_reference.py --feed /path/to/feed.json.gz --out /tmp/ctsg-12h --symbols 8 --pre-hours 12 --forward-hours 12
python3 scripts/audit_causal_hold_forward.py --data /tmp/ctsg-12h --preset presets/secure-minimal.json --start 1792227600 --hours 12 --symbols 8 --out /tmp/ctsg-forward-12h.json
python3 scripts/prepare_signal_reference.py --feed /path/to/feed.json.gz --out /tmp/ctsg-72h --symbols 8 --pre-hours 72 --forward-hours 12
python3 scripts/audit_causal_hold_forward.py --data /tmp/ctsg-72h --preset presets/secure-minimal.json --start 1792227600 --hours 12 --symbols 8 --out /tmp/ctsg-forward-72h.json
python3 scripts/audit_hold_prefix.py --data /tmp/ctsg-12h --preset presets/secure-minimal.json --cut 1792256400 --out /tmp/ctsg-prefix.json
```

The input hashes in the result files should be checked before comparing outcomes. Timing and memory depend on the host and are not live capacity tests.
