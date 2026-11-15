# CTS-G work checklist (updated 2026-11-15 10:15 UTC)

`[x]` done and verified · `[~]` running or partly done · `[ ]` open · `[-]` measured and not shipped (with the reason).
Ship rule for every setting (owner): better than the shipped setting on BOTH the development and the sealed window; an owner instruction is shipped as asked and its measurement reported next to it.
Shipped x02 reference (4 symbols, x02 overlay): executed PF / orders / net **1.054 / 127 / +5.82 % dev, 1.082 / 51 / +3.72 % sealed**.

## Live connections
- [x] x01 stopped by the owner (marker `/var/lib/cts-g/x01-stopped-by-owner`; nothing restarts it).
- [x] x02 (VST demo) runs 150 symbols on the current code (restarted 05:37 UTC); hourly keep-alive reports pid, stats age, valid Sets, positions, PF.
- [x] x02 executed PF recount: plain reading of the trades file equals the engine on every scope (132 closes, PF 0.695 per close, 0.612 USDT).
- [~] x02 loop speed: 0.56 s after the fixes, 6.8 s again at 67 open positions (06:23) -> profile the open-position path (`manage`, controls) next.
- [ ] x02 stage / last-N audit after a full replay rotation (`audit_current_stages.py`) and a live-vs-sim diff per TP step and SL ratio at 150 symbols.

## Performance (owner: "fix all performance issues .. async processings")
- [x] Stats snapshot cached and rebuilt in the background (was 61 % of the trading loop, under the book lock) -> 4 %.
- [x] Forked parallel lane scans (`scanWorkers` 3): one symbol 12.3 s -> 4.4 - 5.3 s, identical rows / aliases / pauses / verdicts.
- [x] Entry picks read a (pack, type) index instead of filtering 16,844 Sets per call.
- [x] Load-governor deadlock after a restart (slow cycles shed the history fetch for good) fixed.
- [ ] Per-Set verdict (`is_eligible` -> `gate_reason`) recomputed on every pick: now the top of the entry path (needs a freshness-aware cache).
- [ ] Open-position path at 60+ positions (x02 06:23: 6.8 s per cycle).

## Hold time (owner)
- [x] Max hold audited: replay = live, an open position keeps its hold, restarts credit the downtime.
- [x] Trailing max hold switched back ON (owner), positions opened while it was off get the hold again.
- [x] Options `trailHoldBars`, `trailHoldArmedFree` (default off) with checks.
- [-] No trailing time exit: 1.028 / 0.881 (worse on both). Armed-free: identical to it.
- [~] Trailing own holds 180 / 720 / 1440 min (rerun with the corrected base, chain10).

## Volume by type (owner: "increase their ratio for vol .. relationally")
- [x] Trailing Sets are the group above 1 everywhere (executed PF dev 1.76 / sealed 2.03 / live 1.99); `volumeByKind` {"trail": 2.0, "exit": 0.5} shipped x01 / x02
      (volume-weighted PF 1.054 -> 1.264 dev, 1.082 -> 1.533 sealed; live x02 USDT 0.505 -> 0.648). Revert = {}.
- [x] Rule documented (CLAUDE.md, every project): pool PF under 1 is normal; post-Base and executed PF decide.
- [x] Same note in the other projects: draft PRs CTS-A-O #274, CTS-K-N #573, CTS-C #25, CTS-X #11 (CTS-A: attaching was refused by the permission check).

## Grid / ranges / SL
- [x] Auto SL range per TP step (on): 1.054 / 1.082 against 0.962 / 0.890 off.
- [x] Min step 2, min trailing arm 3 (Settings).
- [-] Long / wide SL 0.5 - 1.0: 0.736 / 0.633. No Exit Sets: 0.806 dev. TP steps to 50 (Wide): 1.012 / 0.939. Trailing arms to 3.2 %: 0.682 / 0.752.

## Indications and signals (owner: Normal / Long / Wide, invert, other projects, web research)
- [x] Web research: hourly reversal concentrated in crypto, decays within ~4 h (arXiv 2608.21888); Bitcoin intraday time-series momentum (first half hour -> last half hour, Shen / Urquhart / Wang 2022); regime-switch rules: no out-of-sample evidence found.
- [-] Slow 30 min reversion families for Normal / Long: PF 2.0 on other symbols of the same hours, **0.097 on the sealed window**; as engine packs sealed 0.524 (`docs/verification/slow-indications-adaptive-20261115.md`).
- [-] Adaptive direction (follow / invert by the family's recent closes): >= 1 on three windows, **0.54 / 0.49 on the older independent window**; not built.
- [-] CTS-A-O Inverse catalogue (1,812 names, ported in CTS-G): pooled PF 0.37 - 0.85 on all four windows; 1 of 4,025 cells >= 1.1 on all four (10 - 14 trades: chance level); not enabled.
- [~] CTS-A-O Tactic catalogue (9,256 names): four-window screen running; survivors go through trade_sim on dev + sealed.
- [ ] Quarter-hour effect (arXiv 2607.09426) as a time-of-hour indication: not built yet.

## Queued by the owner (2026-11-15, "later")
- [ ] Indications completely for Normal / Long / Wide strategies: additional indications, ranges, tactics, configs, relations and specialities
      (after the 15 / 30 min x relation screen on 25 symbols; same rule: four windows where data exists, then trade_sim dev + sealed against the shipped setting).
- [ ] Trend and break indications calculated over longer distances and timeframes (owner, "later"): longer lookbacks (n 30 .. 120) and 15 / 30 / 60 min series for the
      trend / break families, for the Normal / Long / Wide targets (the CTS-A-O trend tactics on 5 min are the first lead).
- [ ] Axis like the projects where it works (owner): every position-window calculation (prev, last, ongoing / next, pause) and every configuration of them as its own
      independent processing, with a volume ratio relative to the base config Set, fully working and fast. Other projects' records: no Axis above 1 anywhere
      (CTS-C 0 - 28 closes per family at 0.44 - 0.56; CTS-A-O / CTS-X "Axis" is a funded-leg ladder, PF 0.065 - 0.22); here prev-only 1.227 / 1.084 is the best measured.
- [ ] Block strategy as in the projects where it works positively (owner, "later"): CTS-C's 50-symbol run shows Block below Normal for most families; read CTS-A-O / CTS-K-N
      Block records, port the logistic of any positive one, measure dev + sealed.
- [ ] Axis as in the other projects where it works (owner, "later"): re-read CTS-C / CTS-A-O / CTS-X / CTS-K-N Axis code and records, build the working form here
      with its full progression, measure against no Axis and the current Axis on both windows. Current grid: shipped all four 1.000 / 0.783, prev 4 alone 1.227 / 1.084,
      no Axis 1.054 / 1.082 (sealed cont / Block v2 arms rerunning).
- [ ] Micro strategy with trend / break / Low-Range / Inverse indications; Low-Range direction-change calculations (owner, "later").
- [x] 15 / 30 min x relations screen done (25 symbols): 623 of 22,388 cells >= 1.2 on dev and sealed; the top 120 pool to 0.80 (older) / 1.01 (fresh); 4 - 5 hold on all four
      windows (`ao_rvi_n14_t15@c`, `ibs_x14@c`, `macdh_n21_t30@B15`, `ao_frama_n21_t15@C`, `psar_n21_t15@c`) -> trade_sim 25 symbols against the shipped setting (running).
- [~] 15 / 30 min + combined votes x 21 time-activity / situation relations (13,728 names), 25 symbols x 3 days: screen running.
- [~] CTS-A-O trend tactics (`tactic-trend-s` / `-m` 5 min, `tactic-dca-trend-8-atr-1-2-0.25` 1 min; four-window screen PF >= 1.1 everywhere) through trade_sim.

## Tactics / coordinations (chain10, corrected base)
- [~] Trailing first (`trailPick` first-any), trailing + fixed together, Real Negative lane first, Base minimal window "newest negative",
      group gate off, Auto floor low 1.2, execution last-10 check, flow pack off, multi pack off.

## Minimum PF per configuration (owner: own values, Auto with fallback to the manual defaults, no collisions)
- [x] One resolver `floor_of`: own learned floor -> the configuration's own manual floor (Signals, Micro range, lane policy) -> step learned floor -> 1 + TP% x ratio.
- [x] Collisions fixed: Micro range / Signals floors overrode Auto; desk Short Range floor labelled unused under step floors; Signals floor out of the window-size section; window-size class names.
- [x] Check `step-pf.every-coordination-asks-its-own-floor-...`, mutants `own floor:` (3).
- [~] Old against new code on the x02 overlay (4 symbols, dev + sealed): running (chain19).

## Execution last-N check (owner: 10 .. 30 step 5, 50 symbols x 3 days, set best as default)
- [~] trade_sim, 50 symbols (fresh 3-day snapshot 11-12 10:00 .. 11-15 10:00 UTC), x02 overlay with its Axis, execGateN 0 / 10 / 15 / 20 / 25 / 30 on one shared replay (`--cache-shared execGateN`): running (chain18).

## Gates
- [x] Config gate (`configGate`, `configGateN` / `Pf` / `Hours` / `Min`, default off): the group gate's rule on one configuration Set's own executed results (Set x lane);
      live (both entry paths) and simulator share `set_engine.exec_gates_allow`. Check `trader.config-gate-blocks-a-losing-config-and-only-that-config`, mutant `config gate:`.
- [~] Measured on 4 symbols, dev + sealed (N 3 / 5 / 10, floor 1.0 / 1.2, with and without the group gate): running (chain16).

## Axis
- [x] Measured on the current grid (stack_eval, 4 symbols; dev / sealed): no Axis 1.054 (127) / 1.082 (51); all four 1.000 (127) / 0.783 (66); **prev 4 alone 1.227 (121) / 1.084 (51)**;
      cont 1 1.074 sealed; pause 4 0.970 / 1.420; last 1 0.864 sealed.
- [x] **x02 runs Axis prev 4 only** (better than the shipped all four on both windows, not worse than no Axis); copied to /opt, reload confirmed (`axes prev 1, last 0, cont 0, pause 0`).
- [~] Per-Set Axis setting (stack_eval axissets, dev + sealed): running (chain17).

## Block
- [x] Block rounds (count 5, ratio 1.0, step to level 2, ceiling x 3) shipped by the owner (2026-11-11); guard / target share / kinds rules on.
- [-] Block v2 on parents the Axis admits: 0.956 dev against 1.000 without adds: unchanged.

## DCA
- [x] x02: DCA on behind its own Axis gate (owner "run always all"); x01 / presets keep it off.
- [x] DCA add under the venue minimum now learns it (was re-sent every 25 s).
- [-] DCA on the current grid (10 arms, dev + sealed): none better than without on both (best dev rung 0.8 x stop 1.055 / sealed 1.046 against 1.054 / 1.082).

## Correctness / tests
- [x] Full regression 742 / 742 (20 min); npm test; tsc; engine self-test 109 / 109.
- [x] New checks: forked scans, snapshot cache, pack index, load governor, trailing hold (x2), unverified controls, DCA minimum.
- [x] Mutants caught: scan fork (3), load critical (1).
- [ ] Mutants still to run: pack index (timed out under load), trail hold (2).
- [ ] Full regression again after the chain10 / Tactic decisions; CLAUDE.md record of everything above.
