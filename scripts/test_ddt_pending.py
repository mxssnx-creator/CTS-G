"""Drawdown time is valid until a Set has enough prior positions, then evaluated normally."""
import os
import pathlib
import sys
import tempfile
import unittest

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-ddt-test-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from set_engine import SetBook  # noqa: E402


def losing_tape(n, step=60):
    return [{"t": 1000.0 + i * step, "symbol": "A-USDT", "pnl_pct": -0.01, "hold_s": 30} for i in range(n)]


class DdtPendingTests(unittest.TestCase):
    def book(self, **ov):
        book = SetBook()
        book.load(dict(setMaxDdTimeS=600, **ov))
        return book

    def test_too_few_positions_are_valid_and_marked_pending(self):
        book = self.book()
        need = book.eval_need()
        dd = book.ddt_when_enough(losing_tape(need - 1), need, ordered=True)
        self.assertEqual(dd["maxS"], 0.0)
        self.assertEqual(dd.get("pending"), 1.0)
        self.assertLessEqual(dd["maxS"], book.max_dd_s)

    def test_empty_tape_is_valid(self):
        book = self.book()
        self.assertEqual(book.ddt_when_enough([], book.eval_need(), ordered=True)["maxS"], 0.0)

    def test_enough_positions_evaluate_normally_against_the_cap(self):
        book = self.book()
        need = book.eval_need()
        dd = book.ddt_when_enough(losing_tape(need + 40), need, ordered=True)
        self.assertNotIn("pending", dd)
        self.assertGreater(dd["maxS"], book.max_dd_s)  # a permanently losing tape breaches the 10 minute cap

    def test_threshold_is_exactly_the_required_sample(self):
        book = self.book()
        need = book.eval_need()
        self.assertEqual(book.ddt_when_enough(losing_tape(need - 1), need, ordered=True).get("pending"), 1.0)
        self.assertIsNone(book.ddt_when_enough(losing_tape(need), need, ordered=True).get("pending"))

    def test_score_metrics_dd_ok_follows_the_rule(self):
        book = self.book()
        need = book.eval_need()
        short = book._score_metrics([dict(r, source="hist") for r in losing_tape(need - 1)])
        long_ = book._score_metrics([dict(r, source="hist") for r in losing_tape(need + 40)])
        self.assertTrue(short["ddOk"], short.get("max_dd_s"))
        self.assertFalse(long_["ddOk"])


if __name__ == "__main__":
    unittest.main()
