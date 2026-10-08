"""Calculation audit: ratios, percentages, net and cost deductions, and every module that computes them.

Each check recomputes a quantity from first principles (no engine helpers) and compares it with the engine.
Units: prices and PnL are fractions (0.001 = 0.10%); PositionCost (cost) is PERCENT (0.15 = 0.15%).
"""
from __future__ import annotations

import math
import random

from common import COST, SMALL, T0, overlay, synth_bars  # noqa: F401
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
    ok = (abs(neutral["ratio"] - 1.0) < 1e-9 and neutral["pf"] == 0.0
          and abs(even["pf"] - 1.0) < 1e-9 and abs(be["pf"] - (0.001 / 0.0015)) < 1e-3)
    return ok, f"neutral.ratio={neutral['ratio']} neutral.pf={neutral['pf']} even.pf={even['pf']}"


def one_positioncost_is_neutral_gross():
    g = COST / 100.0
    ok = abs(net_pnl_pct(g, COST)) < 1e-12 and abs(signed_result_r(g, COST)) < 1e-9
    return ok, "a gross move of exactly one PositionCost nets 0 and R 0"


def cost_inputs_are_percent_everywhere():
    pairs = [
        ("normalize(0.15)", normalize_cost_pct(0.15) == 0.15),
        ("normalize(3.0) keeps 3%", normalize_cost_pct(3.0) == 3.0),
        ("normalize(0.04)", normalize_cost_pct(0.04) == 0.04),
        ("normalize(0) -> default", normalize_cost_pct(0) == 0.15),
        ("normalize(nan) -> default", normalize_cost_pct(float("nan")) == 0.15),
        ("cost_as_frac(3.0)=0.03", abs(cost_as_frac(3.0) - 0.03) < 1e-12),
        ("step_tp_pct(2,0.15)=0.30%", abs(step_tp_pct(2, 0.15) - 0.003) < 1e-12),
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
    b = SetBook()
    b.load(overlay(positionCostPct=0.04, setMinStep=2, setStepMax=2, setSlRatios=[0.5], trailVariants=["0.3:0.1"],
                   stratIndications=False))
    st = next(s for s in b.by_idx if s.kind == "base")
    lane = SetLane(st, "X")
    cfg = {"entry_conf": 0.5, "time_bars": 10, "scratch_bars": 5, "scratch_min": 0.001,
           "cooldown": 2, "cost_pct": 0.04, "honor_tp": True}
    bars = [[100.0] * 4 + [1.0], [100.0] * 4 + [1.0]]
    lane.scan(0, 2, bars, [1, 0], [0.9, 0.0], True, cfg, [0])
    o = lane.open
    cost = 0.0004
    tp_frac = 2 * cost                                   # k = 2 TP at 0.04% cost = 0.08%
    sl_frac = max(cost, st.tp_pct * st.sl_ratio)         # stop floor is one PositionCost at 0.04%
    ok = (o is not None and abs(o["tp"] - 100.0 * (1 + tp_frac)) < 1e-9
          and abs(o["sl"] - 100.0 * (1 - sl_frac)) < 1e-9)
    return ok, f"cost=0.04 entry sl={o['sl']:.5f} tp={o['tp']:.5f}" if o else "no entry"


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
    risk_pf = vs.pf            # display value: rounded to 3 dp inside LaneScore
    risk_ratio = vs.ratio      # decision value: the unrounded engine PF (4 dp)
    eb = ExitBook()
    ln = LaneScore(key="peak")
    ln.rows = list(rows)
    eb.cost_pct = COST
    eb._score(ln)
    exit_pf = ln.last15_pf
    ok = (abs(stats_pf - want) < 1e-9 and abs(exit_pf - want) < 1e-9
          and abs(risk_ratio - want) < 1e-9 and abs(risk_pf - want) < 5e-4)
    return ok, f"engine={want} stats={stats_pf} variants.decision={risk_ratio} variants.display={risk_pf} exit={exit_pf}"


CHECKS = [
    ("calc.net-is-gross-minus-one-cost", net_is_gross_minus_one_cost),
    ("calc.cost-deducted-exactly-once", cost_deducted_exactly_once),
    ("calc.r-formula-and-identity", r_units_and_identity),
    ("calc.ratio-definition-and-example", ratio_definition_and_example),
    ("calc.pf-matches-reference", pf_matches_reference),
    ("calc.pf-edge-cases", pf_edge_cases),
    ("calc.one-positioncost-is-neutral", one_positioncost_is_neutral_gross),
    ("calc.cost-inputs-are-percent", cost_inputs_are_percent_everywhere),
    ("calc.exit-and-dca-settings-percent", exit_and_dca_settings_are_percent),
    ("calc.gate-default-single-constant", gate_default_is_one_constant),
    ("calc.set-floors-follow-cost", set_floors_follow_the_configured_cost),
    ("calc.trail-stop-named-constant", trail_sets_use_the_named_stop_ratio),
    ("calc.lane-records-from-prices", lane_records_recomputed_from_prices),
    ("calc.same-pf-in-every-module", same_pf_in_every_module),
]
