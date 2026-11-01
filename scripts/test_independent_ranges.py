"""Production-versus-independent stage reconstruction, with deterministic cases."""
import ast
from dataclasses import asdict
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "pulse"))
import set_engine as se
import independent_range_audit as ref


class IndependentRanges(unittest.TestCase):
    def test_standalone_oracle_does_not_import_the_engine(self):
        tree = ast.parse(Path(ref.__file__).read_text())
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        self.assertEqual(set(imports), {"__future__", "bisect", "math"})

    def test_randomized_complete_stage_membership_matches_production(self):
        count = 0
        for full in (False, True):
            for seed in range(40):
                rng = random.Random(seed)
                book = se.SetBook()
                book.load({"setGateWindow": 10, "setGateShortN": 3, "setGateMinTrades": 1,
                           "mainGateN": 10, "mainShortWindows": [2, 5], "mainShortMin": 3,
                           "mainNegWindows": [2, 5], "realGateN": 3, "stageFullWindow": full,
                           "minPfBySize": seed % 2 == 0, "histLookbackBars": 120, "histLookbackAuto": False,
                           "setGateMinN": seed % 3, "mainMinN": (seed + 1) % 3,
                           "setGateMinMode": ("positive", "negative", "avgboth", "both")[seed % 4],
                           "mainMinMode": ("avgpositive", "negative", "both")[seed % 3]})
                params = book.stage_params("general")
                params.max_dd_s = 3600 if seed % 3 else 7200
                params.consistency = 1.05 if seed % 4 == 0 else 0
                times = [600.0 * (i // 2 + 1) for i in range(100)]
                entries = [t - rng.choice((0, 60, 600, 1800)) for t in times]
                nets = [rng.choice((-.009, -.003, .002, .006, .012)) for _ in times]
                now = times[-1] + 1
                expected = se.stage_chain(times, entries, nets, params, window_start=now - params.lookback_s)
                got, rows = ref.reconstruct(times, entries, nets, asdict(params), now)
                for key in ("main", "real", "realneg", "short_n"):
                    self.assertEqual(got[key], expected[key], (seed, full, key))
                self.assertEqual(len(rows), 5)
                count += 1
        self.assertEqual(count, 80)

    def test_future_and_unrelated_lane_changes_do_not_change_past(self):
        b = se.SetBook(); b.load({"histLookbackAuto": False, "mainShortWindows": [5, 10]})
        p = asdict(b.stage_params("general"))
        ts = [float(i * 60) for i in range(200)]
        ent = [t - 30 for t in ts]
        values = [.01 if i % 4 else -.001 for i in range(200)]
        a = ref.reconstruct(ts, ent, values, p, 6001)
        ref.reconstruct(ts, ent, [-.99] * 200, p, 12001)
        other = values[:101] + [-.99] * 99
        b = ref.reconstruct(ts, ent, other, p, 6001)
        self.assertEqual(a, b)

    def test_missing_flat_no_loss_and_finite_high_pf_remain_distinct(self):
        self.assertEqual(ref.figures([])["pfState"], "empty")
        self.assertEqual(ref.figures([0])["pfState"], "flat")
        self.assertEqual(ref.figures([1])["pfState"], "no-loss")
        self.assertEqual(ref.figures([1, -.001])["pf"], 1000)
        with self.assertRaises(ValueError):
            ref.reconstruct([1], [0], [float("nan")], {}, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
