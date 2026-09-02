# Verifier v3 — PF recoordination (base-1) + per-set order/control independence

Created: 2026-09-02. Extends v1/v2 (all v1/v2 checks still apply).

## Scope
1. Systemwide PF coordinations on the new base-1 scale (`position_cost.py`:
   1.00 = neutral = net 0 after 1×PositionCost; each 0.10 = 1×PositionCost;
   system gate default 1.10 = +1×cost). Old-scale stragglers eliminated:
   every `blockProfitFactorRatio` default/fallback 0.8 → 1.1; bounds shifted
   in the same relation (+0.3): floor 0.2 → 0.5 (hard cap 5.0 unchanged);
   desk axis-section Min PF control moved onto the canonical new-scale range
   (min 1 / max 2.3 / step 0.02, matching the main Min PF slider and
   RATIO_MIN/RATIO_MAX/RATIO_STEP).
2. Per-Set independence: for every Set, entry orders and SL/TP control orders
   open independently (unique parseable clientOrderIDs per set/position),
   with correct per-set tracking and stats coordination — proven in-process
   against a fake exchange, plus negative controls.

## Acceptance criteria
- C1 `grep` audit: no `blockProfitFactorRatio` default/fallback of 0.8 remains
  in server/pulse/*.py, src/**, server/pulse/overlay-*.json, scripts/engine-test.py;
  `BLOCK_PF_RATIO_MIN == 0.5`; engine self-test block-min-pf expects 1.11.
- C2 `python3 scripts/engine-test.py` → all pass (incl. updated block_calc
  expectations for pfRatio 1.1 and the new set_orders_test).
- C3 set_orders_test proves, in-process on the real Pulse.place /
  place_ctrl_pair / close_pos code paths with a recording fake exchange:
  (a) N sets → N independent entry orders, each with a unique clientOrderID
      that parse_track() maps back to the correct pack/sl/tr/step/idx;
  (b) each position gets its own independent SL+TP control pair (distinct
      order ids per position, closePosition=true, trigger prices derived from
      that position's own set sl_ratio/step — per-set SL distances differ);
  (c) controls tracked per position (sl_oid/tp_oid on the right Position,
      controls_ok, no cross-position id reuse);
  (d) closing one position cancels only its own symbol's controls and
      attributes the Closed record to the correct set (per-set last15/stats
      coordination; win/loss accounting independent per set);
  (e) sets.snapshot()/coverage() reflect the per-set live closes
      (full stats coordination); sim_stats real/live/sim counts stay correct;
  (f) negative control: a second entry on an occupied symbol is refused
      (one independent book row per symbol), and entry without controls
      scratches (no unprotected live order).
- C4 `python3 -m py_compile server/pulse/*.py` clean.
- C5 `npm run typecheck` (tsc) clean; `npm run build` OK.
- C6 Deploy chain: restore/pulse_trader.py.patch regenerated against pinned
  base b3a9ff3c; applying it yields exactly the new engine blob;
  deploy/linux-common.sh `pt_want` updated to that blob sha; all three hashes
  verified locally.
- C7 All changed files pushed to GitHub main via MCP with per-blob sha
  verification against local `git hash-object` (pulse_trader.py itself is
  NEVER pushed — restore-patch mechanism only).

## Evidence
Run record appended to verifier/runs/2026-09-02-pf-recoordination-set-orders.txt
with commands, exit codes, and observed values (including intermediate runs).
