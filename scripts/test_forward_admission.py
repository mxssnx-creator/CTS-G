"""Independent small-tape checks for the retrospective admission diagnostic."""
from __future__ import annotations

import random
import unittest

import audit_forward_admission as audit
import main_stage_audit as oracle
import set_engine as se


def naive_real(times, entries, nets, params, indices):
    """Plain list scan: no prefix sums, bisect, or engine PF helper."""
    out = set()
    if not params.real_on:
        return set(indices)
    for i in indices:
        prior = [nets[j] for j in indices if entries[i] - params.lookback_s <= times[j] < entries[i]]
        sample = prior[-params.real_n:]
        if len(sample) < (params.real_n if params.full_window else 1):
            continue
        gain = sum(x for x in sample if x > 0)
        loss = sum(-x for x in sample if x < 0)
        pf = gain / loss if loss else 99.0 if gain > 0 else 0.0
        if round(pf, 4) + 1e-9 >= params.floor_for(params.real_n):
            out.add(i)
    return out


class ForwardAdmissionTest(unittest.TestCase):
    def book(self, full):
        b = se.SetBook()
        b.load({"stagesEnabled": True, "mainGateEnabled": True, "mainNegEnabled": False,
                "realGateEnabled": True, "realGateN": 3, "stageFullWindow": full,
                "setGateWindow": 10, "setGateMinTrades": 2, "setGateShortN": 3,
                "mainGateN": 10, "mainShortWindows": [3, 5], "mainShortMin": 3,
                "histLookbackBars": 120, "histLookbackAuto": False,
                "setMinPf": 1.1, "minPfBySize": False})
        return b

    def test_against_naive_causal_reconstruction(self):
        cases = 0
        for full in (False, True):
            params = self.book(full).stage_params("general")
            for seed in range(12):
                rng = random.Random(seed)
                times = [600 * i for i in range(1, 121)]
                entries = [t - rng.choice((60, 600, 1200, 1800)) for t in times]
                nets = [rng.choice((-.009, -.003, 0.0, .002, .006, .012)) for _ in times]
                got = audit.positive_decisions(times, entries, nets, params)
                plain = oracle.naive_chain(times, entries, nets, params)
                self.assertEqual(got["base"], set(plain["main"]))
                self.assertEqual(got["main"], set(plain["real"]))
                self.assertEqual(got["real"], naive_real(times, entries, nets, params, plain["real"]))
                self.assertTrue(got["real"] <= got["main"] <= got["base"])
                cases += 1
        self.assertEqual(cases, 24)

    def test_future_changes_cannot_change_earlier_entry_decisions(self):
        params = self.book(False).stage_params("general")
        times = [600 * i for i in range(1, 101)]
        entries = [t - 600 for t in times]
        original = [.01 if i % 4 else -.005 for i in range(100)]
        altered = original[:60] + [-.99] * 40
        a = audit.positive_decisions(times, entries, original, params)
        b = audit.positive_decisions(times, entries, altered, params)
        for stage in ("base", "main", "real"):
            self.assertEqual({i for i in a[stage] if entries[i] <= times[59]},
                             {i for i in b[stage] if entries[i] <= times[59]})

    def test_unknown_and_negative_lane_fail_closed(self):
        params = self.book(False).stage_params("general")
        with self.assertRaises(ValueError):
            audit.positive_decisions([1.0], [0.0], [float("nan")], params)
        params.neg_on = True
        with self.assertRaises(ValueError):
            audit.positive_decisions([1.0], [0.0], [.01], params)

    def test_profit_factor_keeps_empty_and_no_loss_distinct(self):
        self.assertEqual(audit.stats([])["pfState"], "empty")
        self.assertEqual(audit.stats([.2])["pfState"], "no-loss")
        self.assertIsNone(audit.stats([.2])["pf"])
        self.assertAlmostEqual(audit.stats([.2, -.1])["pf"], 2.0)


if __name__ == "__main__":
    unittest.main()
