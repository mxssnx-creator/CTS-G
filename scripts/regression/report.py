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
    SIGN = {"positive": "+", "negative": "-", "both": "±"}

    def cell(L, S, M, R, stages=True, **flags):
        nonlocal k
        k += 1
        c = {"long": L, "short": S, "main": M, "real": R, "stages": stages, "id": f"L{L}_S{S}_M{M}_R{R}" if stages else f"L{L}_base_alone",
             "short_eff": S if stages else 0, "main_eff": M, "real_eff": R, "valid_pct": 5.0 + k, "eff_pf": 2.0 + k / 10.0,
             "eff_orders_per_hour": 100 + k, "eff_orders": 2400 + 7 * k, "eff_gp": round((2.0 + k / 10.0) * (50.0 + k), 6), "eff_gl": 50.0 + k,
             "exec_pf": round(0.6 + k / 50.0, 3), "exec_orders": 300 + k, "exec_gl_pct": 20.0 + 3 * k, "exec_gp_pct": round(round(0.6 + k / 50.0, 3) * (20.0 + 3 * k), 6),
             "net_pct": -20.0 + k, "max_dd_pct": 30.0 + k,
             "hours_pos": "9/24", "wins": 150 + k, "neg_lane_orders": 0, "half1": {"pf": 0.7, "orders": 150, "net_pct": -5.0},
             "half2": {"pf": 0.9, "orders": 150 + k, "net_pct": -3.0}, "secs": 1.0,
             "windows": {key: {"on": True, "n": 25, "judged_per_hour": 400.0, "sets_per_hour": 20.0 + k, "pct": 5.0 + k, "pf": 1.3 + k / 100.0, "gp": 10.0, "gl": 7.0,
                               "orders": 1000 + k, "mean_set_pf": 1.4} for key in ("baseLong", "baseShort", "mainLong", "mainShort", "mainMinimal", "negative", "real", "realNeg")},
             "hourly": [{"hour": 1_800_000_000 + 3600 * h, "orders": h, "pf": 0.5 + h / 20.0, "net_pct": -1.0 + h / 10.0, "valid_pct": 4.0 + h / 10.0} for h in range(24)]}
        c["windows"]["baseMinimal"] = {"on": False, "n": 0, "judged_per_hour": 0, "sets_per_hour": 0, "pct": 0, "pf": None, "gp": 0, "gl": 0, "orders": 0, "mean_set_pf": None}
        c.update(flags)
        if stages:                      # the id carries every dimension, as the grid's own ids do
            c["id"] += (f"_N{c['neg'].replace(',', '-')}" if c.get("neg") else "") + (f"_m{SIGN[c['min_mode']]}{c['min_n']}" if c.get("min_n") else "") \
                + (f"_k{SIGN[c['main_min_mode']]}{c['main_min_n']}" if c.get("main_min_n") else "")
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
            cell(L, S, 25, 15, real_third=True, main_min_n=1, main_min_mode="positive")      # the Main minimal window: the newest Main result positive
            cell(L, S, 25, 15, real_third=True, neg="off")                                    # the process without the Negative lane
            cell(L, S, 25, 15, real_third=True, min_n=2, min_mode="both")                     # the Base minimal window, mode both: the newest 2 results share one sign
    for L in (20, 30):
        cell(L, None, None, None, stages=False)
    trap = next(c for c in cells if c["id"] == "L20_S5_M25_R25")      # a higher PF on twenty orders: under the floor, never "best"
    trap.update({"exec_pf": 9.9, "exec_orders": 20})
    meta = {"symbols": ["A-USDT", "B-USDT"], "hours": [1_800_000_000 + 3600 * h for h in range(24)], "lookback_bars": 1440, "prehistory_h": 24.0,
            "bars_per_symbol": 3180, "created": 1_800_100_000, "order_floor": 150, "shipped": {"long": 30, "short": 10, "main": 25, "real": 15},
            "longs": [20, 30], "shorts": [5, 10], "mains": [15, 25], "reals": [1, 5, 15, 25], "main_default": 25, "real_default": 15, "min_orders": 0,
            "negs": ["5,10", "1,2", "off"], "neg_default": "5,10",
            "minimals": [{"n": 0, "mode": "", "label": "off"}, {"n": 2, "mode": "negative", "label": "-2"}, {"n": 2, "mode": "both", "label": "±2"}],
            "main_minimals": [{"n": 0, "mode": "", "label": "off"}, {"n": 1, "mode": "positive", "label": "+1"}],
            "max_ddt_h": 24.0, "min_pf": 1.25, "cost_pct": 0.12, "sets": 1538, "self_check": [{"cell": "x", "samples": 10, "bad": 0}], "hold": "off"}
    return {"meta": meta, "cells": cells}


def window_grid_html_has_heatmap_and_best_cell():
    """The window-grid report renders every staged cell once per heatmap panel (thirds x Negative-window sets x Base minimal windows x Main minimal windows), each cell carrying the
    figures of every metric, marks exactly one best cell per panel that holds cells (the highest executed PF among cells with at least the order floor: a
    higher PF on 20 orders is not ranked), flags the shipped cell, writes n/a where the short window is not allowed, embeds the data as valid JSON and makes
    no external request."""
    import window_grid_report as WG
    data = _grid_fixture()
    page = WG.render(data)
    staged = [c for c in data["cells"] if c["stages"]]
    neg_of = lambda c: c.get("neg") or "5,10"  # noqa: E731
    panels = re.findall(r'<section class="panel" data-third="(\w+)" data-neg="([^"]+)" data-min="([^"]+)" data-kmin="([^"]+)" hidden>(.*?)</section>', page, re.S)
    ok = len(panels) == 2 * 3 * 3 * 2
    want_best, got_best = [], []
    for t, neg, mn, kmn, body in panels:
        ok = ok and all(len(v.split("|")) == len(WG.METRICS) for v in re.findall(r'data-v="([^"]+)"', body))
        ids = re.findall(r'data-cell="([^"]+)"', body)
        in_panel = [c for c in staged if c.get("real_third" if t == "real" else "main_third") and neg_of(c) == neg and WG.min_label(c) == mn and WG.main_min_label(c) == kmn]
        ok = ok and set(ids) == {c["id"] for c in in_panel} and len(ids) == len(in_panel)
        ranked = sorted([c for c in in_panel if c["exec_orders"] >= 150], key=lambda c: (c["exec_pf"], c["exec_orders"]), reverse=True)
        want_best += [ranked[0]["id"]] if ranked else []
        got_best += re.findall(r'data-cell="([^"]+)" data-best="1"', body)
    ok = ok and sorted(got_best) == sorted(want_best) and len(want_best) >= 4 and "L20_S5_M25_R25" not in got_best
    ship_cells = set(re.findall(r'<td class="cell[^"]*\bship\b[^"]*" data-cell="([^"]+)"', page))
    ok = ok and "†" in page and "n/a" in page and ship_cells == {"L30_S10_M25_R15"} and 'data-neg="1,2"' in page and 'data-neg="off"' in page and 'data-min="-2"' in page and 'data-min="±2"' in page \
        and "Mean of cell PFs" in page and "Best cell per Negative windows" in page and 'data-kmin="+1"' in page and "Main minimal +1" in page
    emb = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
    emb_ok = bool(emb) and {c["id"] for c in json.loads(emb.group(1).replace("<\\/", "</"))["cells"]} == {c["id"] for c in data["cells"]}
    external = re.findall(r'(?:src|href)=["\']https?://|url\(https?://|@import', page)
    ok = ok and emb_ok and not external
    # the marginal rows are the PF of ALL the orders of the cells that carry the value (gains over losses added over the cells), not a mean of their PFs;
    # the windows tables list every last-N window of the shipped cell and of the best ones, a window that is off reads off
    cs20 = [c for c in staged if c["long"] == 20 and c["exec_pf"] is not None]
    pooled20 = sum(c["exec_gp_pct"] for c in cs20) / sum(c["exec_gl_pct"] for c in cs20)
    mean20 = sum(c["exec_pf"] for c in cs20) / len(cs20)
    row20 = re.search(r"<tr><td>Base long 20</td><td>(\d+)</td><td><b>([\d.]+)</b></td><td>([\d.]+)</td><td>(\d+)</td><td>([\d.]+)</td><td>(\d+)</td>", page)
    eff20 = sum(c["eff_gp"] for c in cs20) / sum(c["eff_gl"] for c in cs20)
    marg_ok = bool(row20) and int(row20.group(1)) == len(cs20) and row20.group(2) == f"{pooled20:.3f}" and row20.group(3) == f"{mean20:.3f}" and abs(pooled20 - mean20) > 0.005 \
        and int(row20.group(4)) == sum(c["exec_orders"] for c in cs20) and row20.group(5) == f"{eff20:.2f}" and int(row20.group(6)) == sum(c["eff_orders"] for c in cs20)
    win_ok = ("Every last-N window on its own" in page and "Main · minimal window" in page and "Base · minimal window" in page and "Shipped setting: long 30" in page
              and re.search(r"<tr class=\"low\"><td>Base · minimal window</td><td>off</td>", page) is not None)
    ok = ok and marg_ok and win_ok
    return ok, (f"panels={len(panels)} best markers={len(got_best)} (want {len(want_best)}) embedded={emb_ok} external={external}; marginal Base long 20: "
                f"{row20.group(2) if row20 else None} (all orders, want {pooled20:.3f}) against the mean of cell PFs {row20.group(3) if row20 else None} (want {mean20:.3f}); windows tables={win_ok}")


def window_grid_files_of_one_window_merge_into_one_report():
    """Several grid files of the same test window (one per set of dimensions) merge into one report: every cell once, the lists of values united, an id in two
    files kept once when the figures agree and refused when they differ, and the merged page equals the page of the unsplit grid."""
    import copy
    import window_grid_report as WG
    whole = _grid_fixture()
    a, b = copy.deepcopy(whole), copy.deepcopy(whole)
    a["cells"] = [c for c in whole["cells"] if not c.get("main_min_n")]
    b["cells"] = [c for c in whole["cells"] if c.get("main_min_n")]
    a["meta"]["main_minimals"] = [{"n": 0, "mode": "", "label": "off"}]
    b["meta"]["main_minimals"] = [{"n": 1, "mode": "positive", "label": "+1"}]
    merged = WG.merge([a, b])
    ids_ok = sorted(c["id"] for c in merged["cells"]) == sorted(c["id"] for c in whole["cells"])
    labels = [m["label"] for m in merged["meta"]["main_minimals"]]
    twice = WG.merge([a, copy.deepcopy(a)])
    dup_once = [c["id"] for c in twice["cells"]] == [c["id"] for c in a["cells"]]
    clash = copy.deepcopy(a)
    clash["cells"][0]["exec_pf"] = (clash["cells"][0]["exec_pf"] or 0) + 0.5
    try:
        WG.merge([a, clash])
        dup_refused = False
    except SystemExit:
        dup_refused = True
    same_page = WG.render(merged) == WG.render({"meta": {**whole["meta"], "main_minimals": merged["meta"]["main_minimals"], "self_check": merged["meta"]["self_check"],
                                                         "groups": merged["meta"]["groups"], "inconsistent_cells": merged["meta"]["inconsistent_cells"]},
                                               "cells": merged["cells"]})
    ok = ids_ok and labels == ["off", "+1"] and dup_once and dup_refused and same_page and 'data-kmin="+1"' in WG.render(merged)
    return ok, f"cells {len(merged['cells'])}/{len(whole['cells'])} ids equal={ids_ok}; Main minimal values {labels}; identical duplicate kept once={dup_once}; clashing duplicate refused={dup_refused}; page equals the unsplit one={same_page}"


def minimal_grid_reports_coordination_without_pf():
    import types
    import window_grid as WGR
    b = types.SimpleNamespace(base_short=5, base_min_n=2, main_on=True, short_on=True, main_min_n=2,
                              neg_on=True, realst_on=True, gate_window=20, main_n=10, short_windows=(5,), neg_windows=(2,), realst_n=5)
    hour = {"windows": {k: [3, 2, 2.0, 1.0, 4, 4.0] for k in WGR.WINDOW_KEYS}}
    result = WGR.window_summary([hour], b, {})
    ok = (result["baseLong"]["pf"] == 2.0 and all(result[k]["role"] == "coordination" and result[k]["pf"] is None
          and result[k]["mean_set_pf"] is None and result[k]["gp"] is None and result[k]["gl"] is None
          and result[k]["sets_per_hour"] == 2 and result[k]["orders"] == 4 for k in ("baseMinimal", "mainMinimal")))
    return ok, f"minimal windows: {[(k, result[k]['sets_per_hour'], result[k]['orders'], result[k]['pf']) for k in ('baseMinimal', 'mainMinimal')]}"


def window_grid_runs_the_process_without_negative_windows_and_minimal_both():
    """The grid's dimensions: the Negative windows include "off" (the process without the Negative lane: mainNegEnabled false, the engine reads it), the minimal
    window runs at every N of 1-3 beside every short window (no ratio rule) and in the modes positive, negative and both (one sign throughout)."""
    import window_grid as WGR
    from set_engine import SetBook
    mins = WGR.parse_minimal("0;+1;-2;±3;b2")
    cells = WGR.enumerate_cells([30], [5, 10], [25], [15], 25, 15, ("5,10", "off"), 0.5, mins, WGR.parse_minimal("0;±1"))
    ids = {c["id"] for c in cells}
    big_under_small = any(c["min_n"] == 3 and c["short"] == 5 for c in cells)              # a minimal window of 3 beside a short window of 5 (and 2 beside none smaller)
    off = next(c for c in cells if c["neg"] == "off" and not c["min_n"] and not c["main_min_n"] and c["short"] == 5)
    both = next(c for c in cells if c["min_mode"] == "both" and c["min_n"] == 3 and c["neg"] == "off" and c["short"] == 5 and not c["main_min_n"])
    po, pb = WGR.cell_patch(off, 1440), WGR.cell_patch(both, 1440)
    bk = SetBook()
    bk.load({**po, "setGateWindow": 30})
    bb = SetBook()
    bb.load({**pb, "setGateWindow": 30})
    ok = (mins == [(0, ""), (1, "positive"), (2, "negative"), (3, "both"), (2, "both")] and big_under_small and "L30_S5_M25_R15_Noff" in ids
          and "L30_S5_M25_R15_Noff_m±3" in ids and po.get("mainNegEnabled") is False and "mainNegWindows" not in po and not bk.neg_on
          and pb["setGateMinMode"] == "both" and bb.base_min_both and bb.base_min_n == 3 and bb.base_short == 5 and bb.neg_on is False)
    return ok, (f"minimals {mins}; N 3 beside short 5={big_under_small}; Negative off patch {po.get('mainNegEnabled')} -> engine neg_on={bk.neg_on}; "
                f"both patch -> engine mode both={bb.base_min_both}, N {bb.base_min_n}, short {bb.base_short}")


CHECKS = [
    ("report.minimal-grid-reports-coordination-without-pf", minimal_grid_reports_coordination_without_pf),
    ("report.window-grid-html-has-heatmap-and-best-cell", window_grid_html_has_heatmap_and_best_cell),
    ("report.window-grid-files-merge-into-one-report", window_grid_files_of_one_window_merge_into_one_report),
    ("report.window-grid-negative-off-and-minimal-both", window_grid_runs_the_process_without_negative_windows_and_minimal_both),
    ("report.hand-computed-pf", hand_computed_pf),
    ("report.summary-pools-sets-and-picks", summary_pools_sets_and_picks_separately),
    ("report.page-embeds-json-and-valid-script", page_embeds_valid_json_and_script),
]
