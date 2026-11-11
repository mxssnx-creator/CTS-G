"""Scheduling changes may change worker assignment, never test coverage."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "regression"))
import run


def plain():
    return True, "ok"


def warm():
    return True, "warm"


warm.__memoized__ = True


class Scheduling(unittest.TestCase):
    def test_long_checks_start_early_and_work_is_balanced(self):
        checks = [(str(i), plain) for i in range(12)]
        durations = {str(i): value for i, value in enumerate([50, 2, 2, 50, 2, 2, 50, 2, 2, 50, 2, 2])}
        groups = run._groups(checks, 3, durations)
        loads = [sum(durations[checks[i][0]] for i in group) for group in groups]
        self.assertLessEqual(max(loads), 100)
        self.assertEqual(sorted(i for g in groups for i in g), list(range(12)))
        self.assertEqual(sorted(g[0] for g in groups), [0, 3, 6])

    def test_memo_chain_stays_ordered_together(self):
        checks = [(str(i), warm if i in (1, 4, 7) else plain) for i in range(9)]
        for workers in (1, 2, 4, 20):
            groups = run._groups(checks, workers, {"4": 100, "0": 200})
            self.assertEqual(sorted(i for g in groups for i in g), list(range(9)))
            owners = [g for g in groups if any(i in (1, 4, 7) for i in g)]
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0][:3], [1, 4, 7])

    def test_missing_or_corrupt_hints_do_not_remove_checks(self):
        checks = [(str(i), plain) for i in range(8)]
        for hints in (None, [], {"0": "bad", "1": None, "2": -1, "3": float("nan"), "4": float("inf")}):
            groups = run._groups(checks, 4, hints)
            self.assertEqual(sorted(i for g in groups for i in g), list(range(8)))
        self.assertEqual(run._groups([], 4, {}), [])


if __name__ == "__main__":
    unittest.main()
