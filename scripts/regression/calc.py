"""Calculation audit: ratios, percentages, net and cost deductions, and every module that computes them.

Each check recomputes a quantity from first principles (no engine helpers) and compares it with the engine.
Units: prices and PnL are fractions (0.001 = 0.10%); PositionCost (cost) is PERCENT (0.15 = 0.15%).
"""
from __future__ import annotations

import math
import random

from common import SMALL, T0, overlay, synth_bars  # noqa: F401

COST = 0.15   # arithmetic examples below are written for 0.15%; independent of the shipped overlay cost
from set_engine import SetBook, SetLane, TRAIL_SL_RATIO, step_tp_pct
from position_cost import GATE_MIN_PF_DEFAULT, cost_as_frac, last_n_cost_pf, net_pnl_pct, normalize_cost_pct, ratio_from_r, signed_result_r


# ---- independent reference formulas, written from the spec -----------------------------------------------------

def ref_net(gross_frac: float, cost_pct: float) -> float:
    return gross_frac - cost_pct / 100.0


def ref_r(gross_frac: float, cost_pct: float) -> float:
    return (gross_frac * 100.0 - cost_pct) / cost_pct


def ref_pf(nets):
    gain = sum(x for x in nets if x > 0)
    loss = sum(-x for x in nets if x < 0)
    if loss > 0:
        return gain / loss
    return 99.0 if gain > 0 else 0.0


def ref_ratio(avg_r: float) -> float:
    return 1.0 + 0.10 * avg_r


def _rows(rnd: random.Random, n: int):
    return [{"pnl_pct": rnd.uniform(-0.02, 0.02)} for _ in range(n)]


def net_is_gross_minus_one_cost():
    rnd = random.Random(1)
    worst = 0.0
    for c in (0.04, 0.15, 0.30):
        for _ in range(400):
            g = rnd.uniform(-0.03, 0.03)
            worst = max(worst, abs(net_pnl_pct(g, c) - ref_net(g, c)))
    return worst < 1e-12, f"max_abs_err={worst:.2e} over 1200 cases"


def cost_deducted_exactly_once():
    gross = 0.003                                    # +0.30% gross
    net = net_pnl_pct(gross, COST)
    ok = abs(net - 0.0015) < 1e-12 and abs(net_pnl_pct(net, COST) - (net - 0.0015)) < 1e-12
    return ok, f"gross 0.30% -> net {net * 100:.4f}% (cost deducted once)"


def r_units_and_identity():
    rnd = random.Random(2)
    worst_formula = worst_identity = 0.0
    for c in (0.04, 0.15, 0.30):
        for _ in range(300):
            g = rnd.uniform(-0.02, 0.02)
            r = signed_result_r(g, c)
            worst_formula = max(worst_formula, abs(r - ref_r(g, c)))
            # net percent = R x cost: the R scale is one PositionCost per unit
            worst_identity = max(worst_identity, abs(r * c - (g * 100.0 - c)))
    ok = worst_formula < 1e-9 and worst_identity < 1e-9
    return ok, f"formula_err={worst_formula:.2e} identity_err={worst_identity:.2e}"


def ratio_definition_and_example():
    rnd = random.Random(3)
    worst = max(abs(ratio_from_r(r) - ref_ratio(r)) for r in [rnd.uniform(-5, 5) for _ in range(300)])
    example = last_n_cost_pf([{"pnl_pct": 0.003}] * 15, 15, COST)["ratio"]   # +1 R each -> 1.10
    ok = worst < 1e-12 and abs(example - 1.10) < 1e-9
    return ok, f"max_err={worst:.2e} example(+1R x15)={example}"


def pf_matches_reference():
    rnd = random.Random(4)
    worst = 0.0
    cases = 0
    for c in (0.04, 0.15, 0.30):
        for n in (5, 15, 50):
            rows = _rows(rnd, n)
            got = last_n_cost_pf(rows, n, c)["pf"]
            want = ref_pf([ref_net(r["pnl_pct"], c) for r in rows])
            worst = max(worst, abs(got - want))
            cases += 1
    return worst < 5e-4, f"cases={cases} max_err={worst:.2e} (engine rounds PF to 4 dp)"


def pf_edge_cases():
    be = last_n_cost_pf([{"pnl_pct": 0.0025}, {"pnl_pct": 0.0}], 2, COST)   # nets +0.10% and -0.15%
    neutral = last_n_cost_pf([{"pnl_pct": COST / 100.0}] * 4, 4, COST)     # every trade nets exactly 0
    even = last_n_cost_pf([{"pnl_pct": 0.0030}, {"pnl_pct": 0.0}], 2, COST)  # nets +0.15% and -0.15%
    ok = (abs(neutral["ratio"] - 1.0) < 1e-9 and neutral["pf"] == 1.0
          and abs(even["pf"] - 1.0) < 1e-9 and abs(be["pf"] - (0.001 / 0.0015)) < 1e-3)
    return ok, f"neutral.ratio={neutral['ratio']} neutral.pf={neutral['pf']} even.pf={even['pf']}"


def pf_is_neutral_at_one_and_ratio_1_1_is_one_positioncost_everywhere():
    """Owner 2026-11-03: PF neutral = 1, ratio 1.1 = an average of +1 PositionCost. Every PF implementation (position_cost.last_n_cost_pf, set_engine.pf_of /
    lane_rank_stats / the gate window, set_block, trade_sim, the simulator's gate) reads 1.0 on net results whose gains equal their losses and on results that
    all net exactly zero (a gross move of exactly one PositionCost), 0 on an empty window, 99 with gains and no loss; the cost ratio is 1.0 at zero net,
    1.10 at +1 PositionCost on average, 0.90 at -1; no gate floor is under MIN_PF_FLOOR 1.02."""
    import os as _o, sys as _s
    import set_engine as se, block_rules as br, position_cost as pc

    def block_pf(vals):
        rec = br.new_record()
        for v in vals:
            br.add_obs(rec, v)
        return br.record_pf(rec)
    from common import ROOT
    _s.path.insert(0, _o.path.join(ROOT, "scripts"))
    import trade_sim as ts
    cf = COST / 100.0
    even = [0.004, -0.004, 0.001, -0.001]          # nets: gains = losses
    flat = [0.0, 0.0, 0.0]
    got = {
        "last_n even": last_n_cost_pf([{"pnl_pct": x + cf} for x in even], 4, COST)["pf"],
        "last_n flat": last_n_cost_pf([{"pnl_pct": cf}] * 3, 3, COST)["pf"],
        "last_n empty": last_n_cost_pf([], 3, COST)["pf"],
        "pf_of even": se.pf_of(0.005, 0.005, 4), "pf_of flat": se.pf_of(0.0, 0.0, 3), "pf_of empty": se.pf_of(0.0, 0.0, 0), "pf_of no loss": se.pf_of(0.01, 0.0, 1),
        "lane_rank even": se.lane_rank_stats(even, cf)[1], "lane_rank flat": se.lane_rank_stats(flat, cf)[1],
        "set_block even": block_pf(even), "set_block flat": block_pf(flat), "set_block empty": block_pf([]),
        "trade_sim even": ts.acc_pf(even), "trade_sim flat": ts.acc_pf(flat),
    }
    want = {"last_n even": 1.0, "last_n flat": 1.0, "last_n empty": 0.0, "pf_of even": 1.0, "pf_of flat": 1.0, "pf_of empty": 0.0, "pf_of no loss": 99.0,
            "lane_rank even": 1.0, "lane_rank flat": 1.0, "set_block even": 1.0, "set_block flat": 1.0, "set_block empty": 0.0, "trade_sim even": 1.0, "trade_sim flat": 1.0}
    bad = {k: v for k, v in got.items() if abs(float(v) - want[k]) > 1e-9}
    # the gate window on prefix sums: all-zero nets -> PF 1.0, so it fails any floor (>= 1.02) but is never read as a loss-only window
    ts_ = [1.0, 2.0, 3.0]
    gpre, lpre = [0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]
    gate_flat = se.gate_ok_at(ts_, [0.0, 0.0, 0.0], gpre, lpre, 10.0, 50, 1, 1.0, 1e9, 1e9, 0.0, 0) if hasattr(se, "gate_ok_at") else None
    one = last_n_cost_pf([{"pnl_pct": 2 * cf}] * 5, 5, COST)                   # every close nets exactly +1 PositionCost
    mixed = last_n_cost_pf([{"pnl_pct": 3 * cf}, {"pnl_pct": cf}], 2, COST)   # +2 and 0 PositionCost: average +1
    neg = last_n_cost_pf([{"pnl_pct": 0.0}] * 2, 2, COST)                      # every close nets -1 PositionCost
    ratio_ok = abs(one["ratio"] - 1.10) < 1e-9 and abs(mixed["ratio"] - 1.10) < 1e-9 and abs(neg["ratio"] - 0.90) < 1e-9 and abs(ratio_from_r(0.0) - 1.0) < 1e-12
    floor_ok = pc.MIN_PF_FLOOR == 1.02 and pc.pf_floor(1.0) == 1.02 and pc.PF_BY_SIZE_DEFAULTS["minPfMicro"] == 1.02
    ok = not bad and ratio_ok and floor_ok and gate_flat in (None, True, False)
    return ok, (f"every PF reads its rule: {not bad} {bad if bad else ''}; ratio +1 PositionCost {one['ratio']} (mixed {mixed['ratio']}), -1 {neg['ratio']}, "
                f"zero {ratio_from_r(0.0)}; floor {pc.MIN_PF_FLOOR}, a configured 1.0 asks {pc.pf_floor(1.0)}, Micro {pc.PF_BY_SIZE_DEFAULTS['minPfMicro']}")


def one_positioncost_is_neutral_gross():
    g = COST / 100.0
    ok = abs(net_pnl_pct(g, COST)) < 1e-12 and abs(signed_result_r(g, COST)) < 1e-9
    return ok, "a gross move of exactly one PositionCost nets 0 and R 0"


def cost_inputs_are_percent_everywhere():
    pairs = [
        ("normalize(0.15)", normalize_cost_pct(0.15) == 0.15),
        ("normalize(3.0) keeps 3%", normalize_cost_pct(3.0) == 3.0),
        ("normalize(0.04)", normalize_cost_pct(0.04) == 0.04),
        ("normalize(0) -> explicit zero", normalize_cost_pct(0) == 0),
        ("normalize(nan) -> default", abs(normalize_cost_pct(float("nan")) - .176) < 1e-12),
        ("cost_as_frac(3.0)=0.03", abs(cost_as_frac(3.0) - 0.03) < 1e-12),
        ("step_tp_pct(2,0.15)=0.20%", abs(step_tp_pct(2, 0.15) - 0.002) < 1e-12 and abs(step_tp_pct(2, 0.04) - 0.002) < 1e-12),
    ]
    bad = [k for k, v in pairs if not v]
    return not bad, f"bad={bad}" if bad else f"{len(pairs)} unit cases"


def exit_and_dca_settings_are_percent():
    from exit_engine import pct_to_frac
    from dca_engine import DcaBook, _pct_list
    checks = [
        ("pct_to_frac(0.015)=0.00015", abs(pct_to_frac(0.015) - 0.00015) < 1e-12),
        ("pct_to_frac(0.15)=0.0015", abs(pct_to_frac(0.15) - 0.0015) < 1e-12),
        ("dca distance 0.05 = 0.05%", abs(_pct_list([0.05], [0.005])[0] - 0.0005) < 1e-12),
    ]
    b = DcaBook()
    b.load({"dcaEnabled": True, "dcaBreakevenProfitPct": 0.04})
    checks.append(("dca breakeven 0.04% -> 0.0004", abs(b.be_pct - 0.0004) < 1e-12))
    bad = [k for k, v in checks if not v]
    return not bad, f"bad={bad}" if bad else f"{len(checks)} percent settings"


def gate_default_is_one_constant():
    from exit_engine import ExitBook
    from dca_engine import DcaBook
    from coord_engine import Coordinator
    from set_engine import SetBook as SB
    vals = {
        "set": SB().min_pf,
        "exit": ExitBook().min_pf,
        "dca": DcaBook().min_pf,
    }
    co = Coordinator()
    co.load({}, {})
    vals["coord"] = co.min_pf
    bad = {k: v for k, v in vals.items() if abs(v - GATE_MIN_PF_DEFAULT) > 1e-12}
    return not bad, f"bad={bad}" if bad else f"all {len(vals)} modules use {GATE_MIN_PF_DEFAULT}"


def set_floors_follow_the_configured_cost():
    """A Set's TP is its step (k x 0.1 %), lifted to PositionCost when the cost is higher; its stop is the ratio x TP, never under PositionCost + one step."""
    ok_all, detail = True, []
    for cost in (0.04, 0.3):
        b = SetBook()
        b.load(overlay(positionCostPct=cost, setMinStep=2, setStepMax=2, setSlRatios=[0.5], trailVariants=["0.3:0.1"],
                       stratIndications=False))
        st = next(s for s in b.by_idx if s.kind == "base")
        lane = SetLane(st, "X")
        cfg = {"entry_conf": 0.5, "time_bars": 10, "scratch_bars": 5, "scratch_min": 0.001,
               "cooldown": 2, "cost_pct": cost, "honor_tp": True}
        bars = [[100.0] * 4 + [1.0], [100.0] * 4 + [1.0]]
        lane.scan(0, 2, bars, [1, 0], [0.9, 0.0], True, cfg, [0])
        o = lane.open
        c = cost / 100.0
        tp_frac = max(c, 0.002)                          # step 2 = 0.2 %; a PositionCost above that lifts the lane's TP to the cost
        sl_frac = max(c + .001, 0.002 * 0.5)                # ratio 0.5 x TP, never under PositionCost + one step = 2 x cost
        ok = (o is not None and abs(o["tp"] - 100.0 * (1 + tp_frac)) < 1e-9 and abs(o["sl"] - 100.0 * (1 - sl_frac)) < 1e-9)
        ok_all = ok_all and ok
        detail.append(f"cost={cost}: sl={o['sl']:.5f} tp={o['tp']:.5f}" if o else f"cost={cost}: no entry")
    return ok_all, "; ".join(detail)


def trail_sets_use_the_named_stop_ratio():
    b = SetBook()
    b.load(overlay(**SMALL))
    tr = next(s for s in b.by_idx if s.kind == "trail")
    lane = SetLane(tr, "X")
    ok = abs(lane.sl_base - tr.tp_pct * TRAIL_SL_RATIO) < 1e-12
    return ok, f"trail stop = TP_mid x {TRAIL_SL_RATIO} (family constant)"


def lane_records_recomputed_from_prices():
    rnd = random.Random(7)
    bars = synth_bars(21, 900, drift=0.0004)
    b = SetBook()
    b.load(overlay(**SMALL))
    cfg = {"entry_conf": 0.5, "time_bars": 20, "scratch_bars": 8, "scratch_min": 0.001,
           "cooldown": 2, "cost_pct": COST, "honor_tp": True}
    dirs = [rnd.choice([0, 0, 1, -1]) for _ in bars]
    confs = [rnd.random() for _ in bars]
    bad = []
    n = 0
    for st in b.by_idx[:40]:
        lane = SetLane(st, "X")
        recs = lane.scan(30, len(bars), bars, dirs, confs, True, cfg)
        for r in recs:
            n += 1
            side = r["dir"]
            gross = (r["exit_px"] - r["entry_px"]) / r["entry_px"] * side
            if abs(gross - r["pnl_pct"]) > 1e-12:
                bad.append((st.id, "gross"))
            if abs(r["pnl"] - ref_net(r["pnl_pct"], COST)) > 1e-12:
                bad.append((st.id, "net"))
            if r["reason"] in ("time", "scratch+") and abs(r["exit_px"] - bars[r["exit_i"]][3]) > 1e-9:
                bad.append((st.id, "exit price is not the bar close"))
            if r["reason"] == "tp" and (side > 0) != (r["exit_px"] > r["entry_px"]):
                bad.append((st.id, "tp on the wrong side"))
    ok = n > 0 and not bad
    return ok, f"trades={n} violations={len(bad)} {bad[:2]}"


def same_pf_in_every_module():
    from stats_report import pf_window
    from risk_variants import VariantBook
    from exit_engine import ExitBook, LaneScore
    rnd = random.Random(9)
    rows = []
    for _ in range(20):
        g = rnd.uniform(-0.01, 0.012)
        rows.append({"pnl_pct": g, "pnl": g * 1000.0, "t": 0, "symbol": "X", "hold_s": 60})
    want = last_n_cost_pf(rows, 15, COST)["pf"]
    stats_pf = pf_window(rows, 15, COST)["pf"]
    vs = VariantBook()._score_rows(rows[-15:], COST)
    risk_pf = vs.pf            # the decision PF (engine 4 dp: the number the gate uses)
    risk_ratio = vs.ratio      # display only: 1 + 0.10 x avgR
    want_ratio = last_n_cost_pf(rows, 15, COST)["ratio"]
    eb = ExitBook()
    ln = LaneScore(key="peak")
    ln.rows = list(rows)
    eb.cost_pct = COST
    eb._score(ln)
    exit_pf = ln.last15_pf
    ok = (abs(stats_pf - want) < 1e-9 and abs(exit_pf - want) < 1e-9
          and abs(risk_pf - want) < 1e-9 and abs(risk_ratio - want_ratio) < 1e-9)
    return ok, f"engine={want} stats={stats_pf} variants.pf={risk_pf} exit={exit_pf} variants.ratio={risk_ratio}"


def payload_pf_fields_are_pf_not_ratio():
    """Every PF a Set row carries is the cost-based PF over that Set's own tape; the ratio is a separate field."""
    from processing import _book
    b = _book(grid_extra=SMALL, n_syms=3, bars=1200)
    b.replay_all(now=T0)
    rows = b.snapshot().get("rows") or []
    checked, bad, differs = 0, [], 0
    for r in rows:
        st = b.sets.get(r["id"])
        tape = st.tape() if st is not None else []
        if not tape:
            continue
        gate = last_n_cost_pf(tape, b.gate_window, b.cost_pct)
        last15 = last_n_cost_pf(tape, b.pf_n, b.cost_pct)
        if abs(r["pf"] - round(gate["pf"], 4)) > 5e-5 or abs(r["pf15"] - round(last15["pf"], 4)) > 5e-5 \
                or abs(r["ratio"] - round(last15["ratio"], 4)) > 5e-5:
            bad.append(r["id"])
            continue
        checked += 1
        differs += int(abs(last15["ratio"] - last15["pf"]) > 1e-6)
    ok = checked > 0 and not bad and differs > 0
    return ok, f"rows={len(rows)} checked={checked} mismatched={len(bad)} pf_differs_from_ratio_in={differs}"


def report_window_is_chronological():
    """The report's last-15 PF is the 15 newest closes, whatever order the export arrives in."""
    from stats_report import build
    rnd = random.Random(21)
    rows = []
    for i in range(40):
        g = rnd.uniform(-0.01, 0.012) if i < 25 else rnd.uniform(-0.002, 0.006)
        rows.append({"pnl_pct": g, "pnl": g * 1000.0, "t": 1000.0 + i, "symbol": "X", "hold_s": 60, "reason": "tp"})
    newest_first = list(reversed(rows))
    blob = build({"closed": newest_first, "sets": {"rows": [], "setCount": 0, "activeCount": 0}, "open": [],
                  "pfCost": {"n": 15}}, cost_pct=COST, conn="x02", now=2000.0)
    want = last_n_cost_pf(rows, 15, COST)["pf"]
    got = blob["profitFactor"]["last15"]["pf"]
    ok = abs(got - want) < 1e-9 and blob["closed"][0]["t"] == max(r["t"] for r in rows)
    return ok, f"report last15={got} newest15={want} export_newest_first={blob['closed'][0]['t'] == max(r['t'] for r in rows)}"


def net_sum_mean_and_pf_share_one_basis():
    """The window's PF, its net (summed over its positions) and its mean net per position come from the same net results: PF above 1 exactly when the net
    is positive, the net is the PF's gross profit less its gross loss, and the mean times the count is the sum. Recomputed here from the gross fractions."""
    rng = random.Random(4)
    bad = []
    for case in range(60):
        n = rng.randint(3, 40)
        gross = [rng.choice((0.0, 0.002, 0.004, -0.003, -0.0015, 0.0012)) + rng.uniform(-0.0004, 0.0004) for _ in range(n)]
        rows = [{"pnl_pct": g, "pnl": 1.0} for g in gross]
        m = last_n_cost_pf(rows, 25, COST)
        win = gross[-25:]
        nets = [ref_net(g, COST) for g in win]
        gp, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
        net_sum = 100.0 * (gp - gl)
        ok = (abs(m["netSumPct"] - net_sum) < 1e-3 and abs(m["netPct"] * m["count"] - net_sum) < 1e-3 * m["count"] + 1e-3
              and abs(m["netPct"] - net_sum / len(win)) < 1e-3
              and ((m["pf"] > 1.0) == (net_sum > 0) or abs(net_sum) < 1e-4))
        if not ok:
            bad.append((case, m["pf"], m["netSumPct"], round(net_sum, 4), m["netPct"]))
    return not bad, f"60 random windows: PF, summed net and mean net per position agree; mismatches {bad[:3]}"


def stage_summary_orders_pf_net_and_hours_share_one_basis():
    """stage_eval.summarize reports the orders opened in the window: its PF, its net, its mean net per order and its hours positive all read those same
    orders, and an hour is the hour a position was opened in (a position that leaves after the window end adds no hour of its own)."""
    import stage_eval as sg
    H = 3600.0
    rows = [{"hour": 0.0, "valid": 1, "total": 10}]
    nets = [0.004, -0.002, 0.003, -0.001, 0.002, -0.0025]
    t_in = [10.0, 20.0, H + 10, H + 20, 2 * H - 5, 2 * H - 4]             # the last two open in hour 1 and leave in hour 2 (after the window end)
    for i, (net, tin) in enumerate(zip(nets, t_in)):
        rows.append({"t": tin + (3 * H if i >= 4 else 60.0), "t_in": tin, "net": net, "legs": []})
    r = sg.summarize(rows, 0.0, 2 * H, False)
    gp, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    hours = {}
    for x, tin in zip(nets, t_in):
        hours[int(tin // H)] = hours.get(int(tin // H), 0.0) + x
    pos = sum(1 for v in hours.values() if v > 0)
    ok = (r["orders"] == 6 and r["pf"] == round(gp / gl, 3) and r["net_pct"] == round(100 * (gp - gl), 2)
          and abs(r["net_per_order_pct"] - round(100 * (gp - gl) / 6, 4)) < 1e-9 and r["hours_positive"] == f"{pos}/{len(hours)}" and len(hours) == 2)
    return ok, f"orders {r['orders']} PF {r['pf']} net {r['net_pct']}% ({r['net_per_order_pct']}% per order) hours positive {r['hours_positive']} (want {pos}/{len(hours)})"


def last_n_window_without_a_full_scan_equals_the_full_scan():
    """The scoring pass reads a long tape from its end (last_n_cost_pf scan_all=False) instead of every row: the window, its PF, R, net sum and count are the same
    as the full scan's for any n, with rows that cannot be measured (no pnl_pct, NaN, text) scattered through the tape, at the end of it and all through it."""
    rng = random.Random(9)
    bad, cases = [], 0
    for trial in range(60):
        n_rows = rng.randint(1, 160)
        rows = []
        for i in range(n_rows):
            kind = rng.random()
            if kind < 0.08:
                rows.append({"t": float(i)})                                    # no pnl_pct
            elif kind < 0.14:
                rows.append({"t": float(i), "pnl_pct": float("nan")})
            elif kind < 0.18:
                rows.append({"t": float(i), "pnl_pct": "x"})
            else:
                rows.append({"t": float(i), "pnl_pct": rng.uniform(-0.01, 0.012)})
        if trial % 5 == 0:
            rows += [{"t": 1e6}, {"t": 1e6 + 1, "pnl_pct": None}]               # unmeasurable rows at the very end
        n = rng.choice((1, 5, 15, 25, 50, 300))
        full = last_n_cost_pf(rows, n, COST)
        fast = last_n_cost_pf(rows, n, COST, scan_all=False)
        cases += 1
        keys = [k for k in full if k != "skipped"]
        if any(full[k] != fast[k] for k in keys) or fast["skipped"] > full["skipped"]:
            bad.append((trial, n))
    return not bad, f"{cases} tapes: windows that differ from the full scan {bad[:4] or 0}"


CHECKS = [
    ("calc.last-n-window-without-a-full-scan-equals-the-full-scan", last_n_window_without_a_full_scan_equals_the_full_scan),
    ("calc.net-sum-mean-and-pf-share-one-basis", net_sum_mean_and_pf_share_one_basis),
    ("calc.stage-summary-orders-pf-net-and-hours-share-one-basis", stage_summary_orders_pf_net_and_hours_share_one_basis),
    ("calc.net-is-gross-minus-one-cost", net_is_gross_minus_one_cost),
    ("calc.cost-deducted-exactly-once", cost_deducted_exactly_once),
    ("calc.r-formula-and-identity", r_units_and_identity),
    ("calc.ratio-definition-and-example", ratio_definition_and_example),
    ("calc.pf-matches-reference", pf_matches_reference),
    ("calc.pf-edge-cases", pf_edge_cases),
    ("calc.one-positioncost-is-neutral", one_positioncost_is_neutral_gross),
    ("calc.pf-is-neutral-at-one-and-ratio-1-1-is-one-positioncost-everywhere", pf_is_neutral_at_one_and_ratio_1_1_is_one_positioncost_everywhere),
    ("calc.cost-inputs-are-percent", cost_inputs_are_percent_everywhere),
    ("calc.exit-and-dca-settings-percent", exit_and_dca_settings_are_percent),
    ("calc.gate-default-single-constant", gate_default_is_one_constant),
    ("calc.set-floors-follow-cost", set_floors_follow_the_configured_cost),
    ("calc.trail-stop-named-constant", trail_sets_use_the_named_stop_ratio),
    ("calc.lane-records-from-prices", lane_records_recomputed_from_prices),
    ("calc.same-pf-in-every-module", same_pf_in_every_module),
    ("calc.payload-pf-fields-are-pf-not-ratio", payload_pf_fields_are_pf_not_ratio),
    ("calc.report-window-is-chronological", report_window_is_chronological),
]
