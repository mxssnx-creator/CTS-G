# Stage entry-history audit (draft)

This is an offline candidate, not a release or a profitability claim. Do not deploy it or merge it as a completed systemwide fix. Automatic-hold splicing, limited exchange history, full-scale throughput and live fills still need independent validation.

## Cause and reproduced defect

At each score, the old implementation dropped every historical close outside the current lookback **before** reconstructing Main and Real membership. An order near that boundary can depend on prior closes that were available when it entered. Rebuilding its admission on the shorter tape can both invent and remove stage members. The engine and `stage_eval.py` shared that error.

On the frozen eight-symbol, 12-hour prehistory / 12-hour forward reference with 0.15% assumed round-trip cost, holds disabled and the two additional indication packs (3,192 Sets), the previous model reported four Base-passing Set/checkpoint observations at 18:00 UTC. They had 50 Base-window closes each. The previous reconstruction showed Main PF 0.495–0.534 on 17–18 closes, below the size-dependent 1.25 Main floor.

One representative entry was 2026-10-17 06:06 UTC. Its entry-time Base window contained 33 available historical closes, PF 0.2285. Cutting that evidence at the later 06:00 scoring boundary left one winning close and admitted the entry on a no-loss sentinel. After using the available earlier entry-time context, each of the four Base-passing observations has one Main close, a loss, and Main PF 0.0. No Main or Real observation passes. The four observations share data; they are not independent trades.

| Offline reference | Base passing observations | Average Base validation | In-sample Base PF | Main / Real passes | Modeled closes |
| --- | ---: | ---: | ---: | ---: | ---: |
| 12h prehistory, 12h forward | 4 / 38,304 | 0.0104% | 1.677 | 0 / 0 | 0 |
| 72h available prehistory, same 12h evaluation lookback and forward window | 13 / 38,304 | 0.0339% | 1.833 | 0 / 0 | 0 |

There is no execution PF in either run. The second row changes available upstream context, not the configured 12-hour evaluation window; it must not be confused with the earlier 72-hour *evaluation-window* experiment. Data include one additional indicator warmup hour. The 12-hour-prehistory feed does not provide all the candidate's requested historical context, so its older decisions are not certified. OHLC replay, assumed costs, overlapping Set windows and a single 12-hour forward period do not establish live performance.

## Candidate change

The candidate constructs stage membership using earlier history, then limits current Base, Main, Real and hold-selection windows to their configured lookback. It keeps the last-N PF floors intact. It also fixes the stage-debug trace to use the configured window-size PF floors. The runtime requests a bounded history-context budget, reports its target and the number of locally short/full symbol histories, and uses a regenerated, hash-verified Pulse restore patch.

Targeted stage tests cover both false admission and false rejection at a lookback boundary, a full replay across expiry, window-scoped short-window voting against an independently calculated oracle, cost and lane independence, and expiry of hold-candidate evidence. The original 3,192-Set historical replay and the longer-context variant each completed the simulator's sampled 60/60 engine/gate consistency check. Both produced zero modeled closes and zero open positions. The candle source was the locally available archived feed identified by SHA-256 `e21d39b0b7583685e2fe355f6261e0860deff3057ca5c81b43a6c3cf374e2f03`; the feed and locally generated shadow caches are not included. Run `trade_sim.py` only with a trusted cache generated for this exact grid and data.

The 3,192-Set grid comes from separate draft #123 at `7db0ea9724fe5984efb4726c6e9fababa946e44f`; it is not present on the Stable-02-01 base of this branch. For this audit the candidate's `set_engine.py` and `stage_eval.py` changes were applied to that reference tree. After generating a fresh shadow cache and reference JSON for the exact grid with `trade_sim.py` (`holdEnabled=false`, `histLookbackBars=720`, `histLookbackAuto=false`, `positionCostPct=0.15`, `extraIndicators=[donchian_break,rsi2_reversion]`, `--last-hours 12 --eval-every 3600 --refresh 0`), copy `scripts/stage_context_audit.py` into that tree and run:

```bash
python scripts/stage_context_audit.py --data DATA_DIR --cache TRUSTED_SHADOW.pkl --reference REFERENCE.json --out STAGES.json
```

The audit records input-file checksums and rejects mismatched symbol counts and the settings recorded by the reference. It cannot verify every setting or prove that an equal-count cache belongs to the data or grid; generate it fresh from trusted inputs. Its 12h results matched the earlier independent recount.

## Release blockers

- The extra replay context increases work. On one BCH symbol, 3,192 Sets and holds off, a cold paper replay took 2.78 s before versus 8.60 s with context; memory peaks were approximately 60 MiB versus 79 MiB. A separate one-symbol, 67-Set automatic-hold trial took 2.04 s cold / 0.20 s warm before versus 8.25 s cold / 0.80 s warm with context, with approximate peaks 28 MiB / 46 MiB. These exploratory, concurrently run measurements are not 50-symbol or live-cycle benchmarks. A four-symbol synthetic paging/startup test took 66 s. The requested sub-500-ms running-cycle target is not demonstrated.
- Longer context can encounter older invalid candles, unavailable history or paging failures; repeated attempts for symbols without sufficient venue history need a bounded and observable policy. The context-count fields describe held bars only, not proof that the venue supplied all required earlier history.
- A fixed context budget cannot prove unlimited-hold positions. The existing automatic-hold method splices candidate tapes; overlap, cooldown, omitted entries, candidate evidence and restart behavior are not fully proven. These need an independently validated causal lane implementation, not forced qualification.
- Symbol, direction and source evidence is still pooled in the underlying engine; the independent CTS-A-O Signals integration and live protective-order reconciliation are incomplete in the separate drafts. This branch does not contain or claim to finish them.
- X01/X02 account state, exchange fee schedules, slippage, control orders and actual execution were not verified. Do not interpret paper PF as exchange PF. No live service was started or restarted.
