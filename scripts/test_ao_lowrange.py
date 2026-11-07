"""Compare the Python port with vectors exported from the actual CTS-A-O source."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "pulse"))
import indication_ao_lowrange as ao
import indication_extra as ix
from set_engine import SetBook


class AOPortTest(unittest.TestCase):
    def test_exact_source_directions(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/ao-lowrange-source.json").read_text())
        compared = fired = 0
        for case in fixture["fixtures"]:
            features = {(w, i): ao.features(case["rows"][:i + 1], w) for w in ao.WINDOWS for i in range(len(case["rows"]))}
            for key, expected in case["expected"].items():
                w, method, relation = key.split("|")
                got = [(i, d) for i in range(len(case["rows"])) if (d := ao.direction(features[int(w), i], method, relation))]
                self.assertEqual(got, [tuple(x) for x in expected], key)
                compared += len(case["rows"])
                fired += len(got)
        self.assertEqual(compared, 390000)
        self.assertGreater(fired, 1000)

    def test_registration_and_isolation(self):
        self.assertEqual(len(ao.LOW_RANGE), 1950)
        names = ["ao_lr3_mid-reclaim_standalone_t1", "ao_lr12_breakout_trend_t5", "ao_lr24_failed-break_move-reverse_t15"]
        b = SetBook()
        b.load({"directionMode": "split", "indicationConfigs": [{"names": names, "stepMin": 2, "stepMax": 12,
                 "slRatios": [.5, 1], "types": ["base", "exit", "trail"]}], "positionCostPct": .11})
        self.assertTrue(all(ix.known(n) for n in names))
        ids = [st.id for st in b.by_idx]
        self.assertEqual(len(ids), len(set(ids)))
        for name in names:
            states = [st for st in b.by_idx if st.pack == "extra." + name]
            self.assertTrue(states)
            self.assertEqual({st.kind for st in states}, {"base", "exit", "trail"})
            self.assertEqual({st.side for st in states}, {-1, 1})
            self.assertTrue({2, 4, 7}.issubset({st.step for st in states if st.kind == "base"}))

    def test_invalid_current_bar_and_symbol_memo(self):
        rows = [[100, 100.1, 99.9, 100, 1] for _ in range(20)]
        up = rows + [[100, 100.4, 100, 100.3, 2]]
        down = rows + [[100, 100, 99.6, 99.7, 2]]
        fn = ao.make_signal(6, "breakout", "standalone", 1)
        self.assertEqual(fn(up)[0], 1)
        self.assertEqual(fn(down)[0], -1)
        self.assertEqual(fn(up)[0], 1)
        self.assertIsNone(fn(rows + [[100, 100, float("nan"), 100, 1]]))


if __name__ == "__main__":
    unittest.main()
