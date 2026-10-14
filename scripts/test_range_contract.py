"""Range contract: the deployed profile's ranges are identical in the overlays,
the replay SetBook and the live IndicationBook, and every grid is evaluable
(no TP clipped onto one value, no range without its own replay evidence)."""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-range-"))
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
import connection_profile as cp  # noqa: E402
import indication_engine as ie  # noqa: E402
from position_cost import SL_TP_RATIOS  # noqa: E402
from set_engine import SetBook, step_tp_pct  # noqa: E402

LANES = ("bingx-x01", "bingx-x02")
EXTRA_RANGES = {"trend": ("indTrendRanges", "trendRanges", (13, 21, 34)),
                "break": ("indBreakRanges", "breakRanges", (8, 16, 32)),
                "move": ("indMoveRanges", "moveRanges", (20, 30, 40))}


def overlay(lane):
    ov = json.loads((ROOT / f"server/pulse/overlay-{lane}.json").read_text())
    ov.update(cp.processing_profile())
    return ov


def overlay_key(setting_key):
    return "ind" + setting_key[0].upper() + setting_key[1:]


class RangeContract(unittest.TestCase):
    def test_indication_ranges_agree_in_overlay_replay_and_live(self):
        for lane in LANES:
            ov = overlay(lane)
            book = SetBook()
            book.load(ov, rebuild=False)
            live = ie.IndicationBook()
            live.load(ov)
            for kind, (skey, default, _) in ie.RANGE_KINDS.items():
                want = list(ov.get(overlay_key(skey)) or default)
                self.assertEqual(list(book.ind_settings[skey]), want, f"{lane} replay {kind}")
                self.assertEqual(list(live.settings[skey]), want, f"{lane} live {kind}")
                self.assertEqual(want, list(default), f"{lane} overlay {kind} drifted from the engine default")
            for kind, (okey, skey, default) in EXTRA_RANGES.items():
                want = list(ov.get(okey) or default)
                self.assertEqual(list(book.ind_settings[skey]), want, f"{lane} replay {kind}")
                self.assertEqual(list(live.settings[skey]), want, f"{lane} live {kind}")

    def test_tp_grid_spans_the_steps_without_clipping(self):
        for lane in LANES:
            ov = overlay(lane)
            self.assertAlmostEqual(float(ov["tpStepPct"]), 0.1)
            lo, hi = int(ov["setMinStep"]), int(ov["setStepMax"])
            unit = float(ov["tpStepPct"])
            self.assertGreaterEqual(lo * unit, float(ov["tpMinPct"]) - 1e-9)
            tp_max = float(ov["tpMaxPct"])
            if tp_max > 0:
                self.assertLessEqual(hi * unit, tp_max + 1e-9, f"{lane}: top steps would collapse onto tpMaxPct")
            book = SetBook()
            book.load(ov)
            tps = sorted({round(st.tp_pct, 8) for st in book.sets.values()
                          if st.kind == "base" and abs(st.sl_ratio - 2.0) < 1e-9})
            # SL:TP 2.0 never binds the SL floor: one TP per step, step x unit.
            self.assertEqual(tps, [round(step_tp_pct(s, unit / 100.0), 8) for s in range(lo, hi + 1)])

    def test_tp_grid_does_not_follow_the_position_cost(self):
        ov = overlay("bingx-x02")
        a, b = SetBook(), SetBook()
        a.load({**ov, "positionCostPct": 0.10})
        b.load({**ov, "positionCostPct": 0.18})
        self.assertEqual(sorted(st.tp_pct for st in a.sets.values()), sorted(st.tp_pct for st in b.sets.values()))

    def test_sl_tp_ratio_and_trailing_grids(self):
        for lane in LANES:
            ov = overlay(lane)
            self.assertEqual(tuple(round(float(x), 1) for x in ov["slToTpRatios"]), SL_TP_RATIOS)
            book = SetBook()
            book.load(ov, rebuild=False)
            arms = sorted({a for _, a, _ in book.trails})
            gives = sorted({g for _, _, g in book.trails})
            self.assertEqual(arms, [0.6, 0.9, 1.2, 1.5])
            self.assertEqual(gives, [0.2, 0.3, 0.4, 0.5])

    def test_primary_move_range_has_its_own_replay_lane(self):
        book = SetBook()
        book.load({**overlay("bingx-x02"), "histLookbackBars": 400}, rebuild=False)
        bars = []
        px = 100.0
        for i in range(400):
            step = 0.25 if (i // 25) % 2 == 0 else -0.25
            nxt = px * (1 + step / 100)
            bars.append([px, max(px, nxt) * 1.0005, min(px, nxt) * 0.9995, nxt, 100.0 + (i % 7) * 40])
            px = nxt
        book.ingest_bars("R-USDT", bars)
        _, kind_sigs, _ = book.prepare_replay_signals("R-USDT", now=time.time())
        self.assertNotIn("move|", "".join(k for k in kind_sigs if k == "move|"))
        configs = {k for k in kind_sigs if k.startswith("move|")}
        self.assertIn("move|move:30", configs)
        self.assertFalse(any(d for d, _ in kind_sigs["move"]), "primary Move votes now ride the move:30 lane")


if __name__ == "__main__":
    unittest.main()
