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


CHECKS = [
    ("report.hand-computed-pf", hand_computed_pf),
    ("report.summary-pools-sets-and-picks", summary_pools_sets_and_picks_separately),
    ("report.page-embeds-json-and-valid-script", page_embeds_valid_json_and_script),
]
