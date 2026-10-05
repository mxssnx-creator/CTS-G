"""All four coordination axes ship enabled and qualify purely by PF evidence.

- the connection profile + both overlays load with prev/last/cont/pause on;
- an empty config still defaults to off (unchanged code-level default);
- axis children qualify / block / pause from the parent's own closed tape;
- the Block/DCA add gate applies last-N PF and the pause per configuration;
- the cont axis halves the add stack only while recent PF is below the floor;
- entries keep the full maxOpen cap whenever qualified Sets exist (axes are
  advisory for entries, never a hard book cap).
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-axes-"))
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
from coord_engine import AXIS_SPECS, Coordinator  # noqa: E402
from connection_profile import processing_profile  # noqa: E402

COST = 0.1
AXES = ("prev", "last", "cont", "pause")


def move(pf):
    """Gross price-move fraction whose single-row cost-PF equals ``pf``."""
    return (1.0 + (pf - 1.0) / 0.1) * COST / 100.0


def rows(moves, t0=1000.0):
    return [{"t": t0 + i * 60, "symbol": "X-USDT", "side": "LONG", "pnl_pct": m,
             "pnl": m * 100.0, "hold_s": 60} for i, m in enumerate(moves)]


def coordinator(**extra):
    ov = {**processing_profile(), "positionCostPct": COST, **extra}
    c = Coordinator()
    c.load({}, ov)
    return c


class AxesEnabled(unittest.TestCase):
    def test_shipped_configs_enable_all_four_axes(self):
        prof = processing_profile()
        for axis in AXES:
            self.assertTrue(prof[f"axis{axis.title()}Enabled"], axis)
        for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
            ov = json.loads((ROOT / "server/pulse" / name).read_text())
            c = Coordinator()
            c.load({}, ov)
            self.assertTrue(c.axes_active(), name)
            for axis in AXES:
                self.assertTrue(c.axes[axis].enabled, (name, axis))
                self.assertLessEqual(c.axes[axis].max_window, AXIS_SPECS[axis]["max"])

    def test_empty_config_default_stays_off(self):
        c = Coordinator()
        c.load({}, {})
        self.assertFalse(c.axes_active())
        self.assertEqual(c.axis_variants("p", rows([move(1.5)] * 20)), [])

    def test_children_qualify_by_pf(self):
        c = coordinator()
        good = c.axis_variants("parent-good", rows([move(1.5)] * 30))
        self.assertTrue(good)
        self.assertEqual({v["axis"] for v in good}, set(AXES))
        self.assertTrue(all(v["qualified"] for v in good if v["closedN"] >= min(3, v["relativeCount"])))
        bad = coordinator().axis_variants("parent-bad", rows([move(0.7)] * 30))
        self.assertTrue(bad)
        self.assertFalse(any(v["qualified"] for v in bad), "a losing tape produced a qualified axis child")
        self.assertTrue(all(v["parentSetId"] == "parent-bad" for v in bad))

    def test_pause_axis_freezes_after_consecutive_losses(self):
        c = coordinator()
        tape = rows([move(1.6)] * 20 + [-0.004] * 8)
        pause = [v for v in c.axis_variants("p", tape) if v["axis"] == "pause"]
        self.assertTrue(any(v["paused"] for v in pause))
        self.assertFalse(any(v["qualified"] for v in pause if v["paused"]))

    def test_add_gate_applies_last_pf_and_pause_per_config(self):
        c = coordinator()
        ok, reasons, _ = c.add_gate(rows([move(1.6)] * 20), 0, count=2, count_tape=[0.01, 0.02, 0.015])
        self.assertTrue(ok, reasons)
        weak, reasons, _ = c.add_gate(rows([move(1.6)] * 20), 0, count=2, count_tape=[0.01, -0.03, -0.02, -0.01])
        self.assertFalse(weak)
        self.assertTrue(any("count-pos" in r for r in reasons), reasons)
        paused, reasons, _ = c.add_gate(rows([move(1.6)] * 10 + [-0.004] * 8), 8)
        self.assertFalse(paused)
        self.assertTrue(any("pause" in r for r in reasons), reasons)

    def test_cont_axis_halves_add_stack_only_on_weak_pf(self):
        c = coordinator()
        self.assertEqual(c.add_stack_cap(6, 1.5), 6)
        self.assertEqual(c.add_stack_cap(6, 0.9), 3)
        off = coordinator(axisContEnabled=False)
        self.assertEqual(off.add_stack_cap(6, 0.9), 6)


if __name__ == "__main__":
    unittest.main()
