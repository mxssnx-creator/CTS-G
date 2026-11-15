# CTS-G work checklist (updated 2026-11-15 07:00 UTC)

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

## Tactics / coordinations (chain10, corrected base)
- [~] Trailing first (`trailPick` first-any), trailing + fixed together, Real Negative lane first, Base minimal window "newest negative",
      group gate off, Auto floor low 1.2, execution last-10 check, flow pack off, multi pack off.

## Axis
- [x] x02 runs all four Axis controls (prev 12, last 4, cont 8, pause 8).
- [~] Measured on the current grid (stack_eval, dev, base without trailing time exit): no Axis 1.028 / drawdown 27.2 %, **Axis all four 1.239 / drawdown 14.4 %**,
      coordinator floor 1.122; sealed part rerunning on the corrected base.
- [ ] Ship / keep decision once the sealed part is in (rule: both windows).

## Block
- [x] Block rounds (count 5, ratio 1.0, step to level 2, ceiling x 3) shipped by the owner (2026-11-11); guard / target share / kinds rules on.
- [~] Block v2 on parents the shipped Axis admits (stack_eval block2, dev + sealed) rerunning on the corrected base.
- [ ] Decision against "without" on both windows.

## DCA
- [x] x02: DCA on behind its own Axis gate (owner "run always all"); x01 / presets keep it off.
- [x] DCA add under the venue minimum now learns it (was re-sent every 25 s).
- [ ] Measure DCA on the current grid (stack_eval `--part dca`, dev + sealed); earlier measurements (11-04) never beat "without".

## Correctness / tests
- [x] Full regression 742 / 742 (20 min); npm test; tsc; engine self-test 109 / 109.
- [x] New checks: forked scans, snapshot cache, pack index, load governor, trailing hold (x2), unverified controls, DCA minimum.
- [x] Mutants caught: scan fork (3), load critical (1).
- [ ] Mutants still to run: pack index (timed out under load), trail hold (2).
- [ ] Full regression again after the chain10 / Tactic decisions; CLAUDE.md record of everything above.
