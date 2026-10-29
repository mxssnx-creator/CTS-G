"""Report suite: the 24 h simulation report's numbers and its HTML page."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from common import ROOT, Skip

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import sim_report as SR  # noqa: E402
import sim_report_html as SH  # noqa: E402


def _res():
    sets = [
        {"id": "a", "pack": "shortrange", "kind": "base", "step": 2, "sl_ratio": 1.0, "trail": "", "validated_hours": 12,
         "shadow_n": 10, "shadow_pf": 0.8, "orders": 4, "exec_pf": 2.0, "exec_gp": 0.04, "exec_gl": 0.02},
        {"id": "b", "pack": "shortrange", "kind": "base", "step": 2, "sl_ratio": 2.0, "trail": "", "validated_hours": 0,
         "shadow_n": 10, "shadow_pf": 0.5, "orders": 0, "exec_pf": 0.0, "exec_gp": 0.0, "exec_gl": 0.0},
        {"id": "c", "pack": "shortrange", "kind": "base", "step": 3, "sl_ratio": 1.0, "trail": "", "validated_hours": 24,
         "shadow_n": 10, "shadow_pf": 1.2, "orders": 2, "exec_pf": 0.5, "exec_gp": 0.01, "exec_gl": 0.02},
    ]
    def tr(step, net, hold, h=0, sym="A-USDT", reason="tp"):
        return {"symbol": sym, "pack": "shortrange", "kind": "base", "step": step, "sl_ratio": 1.0, "trail": "", "set_id": "a",
                "side": "LONG", "hour": h, "t_in": 1_800_000_000_000, "t_out": 1_800_000_060_000, "entry": 1.0, "exit": 1.01,
                "reason": reason, "hold_s": hold, "gross": net + 0.12, "net": net, "open_end": False}
    trades = [tr(2, 0.24, 60), tr(2, -0.12, 120), tr(2, 0.36, 180), tr(3, -0.2, 300)]
    meta = {"symbols": ["A-USDT"], "hours": 24, "cost_pct": 0.12, "min_pf": 1.1, "gate_window": 50, "gate_min": 30,
            "max_ddt_h": 12.0, "packs": ["shortrange"], "window_start": 1_800_000_000_000, "window_end": 1_800_086_400_000,
            "sl_floor_steps": 1, "runtime_s": 1.0, "sets": 3}
    return {"meta": meta, "trades": trades, "sets": sets,
            "validated": [{"hour": h, "by_set": {"a": h < 12, "b": False, "c": True}} for h in range(24)]}


def hand_computed_pf():
    got = [SR.pf_of([0.24, -0.12, 0.36]), SR.pf_of([0.1]), SR.pf_of([-0.1]), SR.pf_of([])]
    return got == [5.0, 99.0, 0.0, 0.0], f"pf={got}"


def summary_pools_sets_and_picks_separately():
    s = SR.summarize(_res())
    r2 = next(r for r in s["rows"] if r["step"] == 2)
    r3 = next(r for r in s["rows"] if r["step"] == 3)
    ok = (r2["sets"] == 2 and abs(r2["validated_pct_hours"] - 100.0 * 12 / 48) < 1e-9 and r2["validated_pct_ever"] == 50.0
          and r2["set_orders"] == 4 and abs(r2["set_pf"] - 2.0) < 1e-9
          and r2["orders"] == 3 and abs(r2["pf"] - 5.0) < 1e-9 and abs(r2["net_pct"] - 0.48) < 1e-9
          and abs(r2["win_rate"] - 66.7) < 1e-9 and abs(r2["avg_hold_min"] - 2.0) < 1e-9
          and r3["orders"] == 1 and r3["pf"] == 0.0 and abs(r3["set_pf"] - 0.5) < 1e-9
          and s["overall"]["orders"] == 4)
    return ok, f"step2={ {k: r2[k] for k in ('validated_pct_hours', 'set_orders', 'set_pf', 'orders', 'pf', 'net_pct')} } step3 pf={r3['pf']}"


def page_embeds_valid_json_and_script():
    res = _res()
    res["summary"] = SR.summarize(res)
    page = SH.render(res)
    m = re.search(r"const R=(\{.*?\});\nconst T=", page, re.S)
    data = json.loads(m.group(1)) if m else {}
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    ok_js = None
    if shutil.which("node") and scripts:
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "page.js")
            open(p, "w").write(scripts[0])
            ok_js = subprocess.run(["node", "--check", p], capture_output=True, text=True).returncode == 0
    ok = ("__DATA__" not in page and data.get("meta", {}).get("sets") == 3 and len(data.get("trades", [])) == 4
          and "Per pack and TP step" in page and "heat" in page and ok_js in (True, None))
    return ok, f"embedded trades={len(data.get('trades', []))} script syntax ok={ok_js} bytes={len(page)}"


def _grid_fixture():
    """A small window grid: Base long 20/30 x short 5/10 x Real 15/25 (Main static 25) and Main static 15/25 (Real 15), plus the long windows alone."""
    cells, k = [], 0

    def cell(L, S, M, R, stages=True, **flags):
        nonlocal k
        k += 1
        c = {"long": L, "short": S, "main": M, "real": R, "stages": stages, "id": f"L{L}_S{S}_M{M}_R{R}" if stages else f"L{L}_base_alone",
             "short_eff": S if stages else 0, "main_eff": M, "real_eff": R, "valid_pct": 5.0 + k, "eff_pf": 2.0 + k / 10.0,
             "eff_orders_per_hour": 100 + k, "exec_pf": round(0.6 + k / 50.0, 3), "exec_orders": 300 + k, "net_pct": -20.0 + k, "max_dd_pct": 30.0 + k,
             "hours_pos": "9/24", "wins": 150 + k, "neg_lane_orders": 0, "half1": {"pf": 0.7, "orders": 150, "net_pct": -5.0},
             "half2": {"pf": 0.9, "orders": 150 + k, "net_pct": -3.0}, "secs": 1.0,
             "hourly": [{"hour": 1_800_000_000 + 3600 * h, "orders": h, "pf": 0.5 + h / 20.0, "net_pct": -1.0 + h / 10.0, "valid_pct": 4.0 + h / 10.0} for h in range(24)]}
        c.update(flags)
        cells.append(c)
        return c
    for L in (20, 30):
        for S in (5, 10):
            if S >= L:
                continue
            for R in (15, 25):
                cell(L, S, 25, R, real_third=True, **({"main_third": True} if R == 15 else {}))
            cell(L, S, 15, 15, main_third=True)
            for R in (1, 5):
                cell(L, S, 25, R, real_third=True, neg="1,2")             # the Negative windows 1,2 and the ongoing windows 1 and 5
            cell(L, S, 25, 15, real_third=True, min_n=2, min_mode="negative")      # the Base minimal window: the newest 2 results all negative
    for L in (20, 30):
        cell(L, None, None, None, stages=False)
    trap = next(c for c in cells if c["id"] == "L20_S5_M25_R25")      # a higher PF on twenty orders: under the floor, never "best"
    trap.update({"exec_pf": 9.9, "exec_orders": 20})
    meta = {"symbols": ["A-USDT", "B-USDT"], "hours": [1_800_000_000 + 3600 * h for h in range(24)], "lookback_bars": 1440, "prehistory_h": 24.0,
            "bars_per_symbol": 3180, "created": 1_800_100_000, "order_floor": 150, "shipped": {"long": 30, "short": 10, "main": 25, "real": 15},
            "longs": [20, 30], "shorts": [5, 10], "mains": [15, 25], "reals": [1, 5, 15, 25], "main_default": 25, "real_default": 15, "min_orders": 0,
            "negs": ["5,10", "1,2"], "neg_default": "5,10",
            "minimals": [{"n": 0, "mode": "", "label": "off"}, {"n": 2, "mode": "negative", "label": "-2"}],
            "max_ddt_h": 24.0, "min_pf": 1.25, "cost_pct": 0.12, "sets": 1538, "self_check": [{"cell": "x", "samples": 10, "bad": 0}], "hold": "off"}
    return {"meta": meta, "cells": cells}


def window_grid_html_has_heatmap_and_best_cell():
    """The window-grid report renders every staged cell once per heatmap panel (thirds x Negative-window sets x Base minimal windows), each cell carrying the
    figures of every metric, marks exactly one best cell per panel that holds cells (the highest executed PF among cells with at least the order floor: a
    higher PF on 20 orders is not ranked), flags the shipped cell, writes n/a where the short window is not allowed, embeds the data as valid JSON and makes
    no external request."""
    import window_grid_report as WG
    data = _grid_fixture()
    page = WG.render(data)
    staged = [c for c in data["cells"] if c["stages"]]
    neg_of = lambda c: c.get("neg") or "5,10"  # noqa: E731
    panels = re.findall(r'<section class="panel" data-third="(\w+)" data-neg="([^"]+)" data-min="([^"]+)" hidden>(.*?)</section>', page, re.S)
    ok = len(panels) == 2 * 2 * 2
    want_best, got_best = [], []
    for t, neg, mn, body in panels:
        ok = ok and all(len(v.split("|")) == len(WG.METRICS) for v in re.findall(r'data-v="([^"]+)"', body))
        ids = re.findall(r'data-cell="([^"]+)"', body)
        in_panel = [c for c in staged if c.get("real_third" if t == "real" else "main_third") and neg_of(c) == neg and WG.min_label(c) == mn]
        ok = ok and set(ids) == {c["id"] for c in in_panel} and len(ids) == len(in_panel)
        ranked = sorted([c for c in in_panel if c["exec_orders"] >= 150], key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)
        want_best += [ranked[0]["id"]] if ranked else []
        got_best += re.findall(r'data-cell="([^"]+)" data-best="1"', body)
    ok = ok and sorted(got_best) == sorted(want_best) and len(want_best) >= 4 and "L20_S5_M25_R25" not in got_best
    ship_cells = set(re.findall(r'<td class="cell[^"]*\bship\b[^"]*" data-cell="([^"]+)"', page))
    ok = ok and "†" in page and "n/a" in page and ship_cells == {"L30_S10_M25_R15"} and 'data-neg="1,2"' in page and 'data-min="-2"' in page \
        and "Mean exec PF" in page and "Best cell per Negative windows" in page
    emb = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
    emb_ok = bool(emb) and {c["id"] for c in json.loads(emb.group(1).replace("<\\/", "</"))["cells"]} == {c["id"] for c in data["cells"]}
    external = re.findall(r'(?:src|href)=["\']https?://|url\(https?://|@import', page)
    ok = ok and emb_ok and not external
    return ok, f"panels={len(panels)} best markers={len(got_best)} (want {len(want_best)}) embedded={emb_ok} external={external}"


CHECKS = [
    ("report.window-grid-html-has-heatmap-and-best-cell", window_grid_html_has_heatmap_and_best_cell),
    ("report.hand-computed-pf", hand_computed_pf),
    ("report.summary-pools-sets-and-picks", summary_pools_sets_and_picks_separately),
    ("report.page-embeds-json-and-valid-script", page_embeds_valid_json_and_script),
]
