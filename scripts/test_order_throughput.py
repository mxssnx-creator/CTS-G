#!/usr/bin/env python3
"""The order lane is as free as the venue allows, and it is spent best-first.

BingX allows 10 order placements per second per IP (since 2025-10-16). Mainnet and
VST can share one IP, so each lane gets 4.0 / s. The entry window examines many
cheap candidates per cycle and the matrix keeps the coordinated (best first) order.
"""
import json
import pathlib
import sys
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "pulse"))
import bingx_fast  # noqa: E402
from bingx_fast import BATCH_RPS, FastBingX, LIMITS, ORDER_LANES, ORDER_RPS, REPLACE_RPS, TokenBucket  # noqa: E402
from connection_profile import BATCH_RPS as PROFILE_BATCH_RPS, ORDER_RPS as PROFILE_ORDER_RPS, processing_profile  # noqa: E402
from entry_dispatch import EntryMatrix  # noqa: E402
from load_engine import LoadGovernor, SMALL_BOOK_ENTRY_BATCH  # noqa: E402

VENUE_ORDERS_PER_SECOND_PER_IP = 10.0
VENUE_BATCHES_PER_SECOND = 5.0  # batchOrders quota (ordinary users), up to 5 orders each


class OrderLane(unittest.TestCase):
    def test_one_ceiling_everywhere_and_headroom_for_two_lanes(self):
        limits = json.loads((ROOT / "server/pulse/system-limits.json").read_text())
        default, low, high, _integer = limits["systemOrderRps"]
        self.assertEqual({default, high, LIMITS["order"][0], PROFILE_ORDER_RPS}, {ORDER_RPS})
        self.assertEqual(processing_profile()["systemOrderRps"], ORDER_RPS)
        self.assertLessEqual(2 * ORDER_RPS, 0.85 * VENUE_ORDERS_PER_SECOND_PER_IP, "two lanes on one IP")
        self.assertLess(REPLACE_RPS, ORDER_RPS, "the tighter pacing bucket still binds ordinary orders")

    def test_a_user_setting_can_lower_but_never_exceed_the_ceiling(self):
        api = FastBingX.__new__(FastBingX)
        api.buckets = {k: TokenBucket(*v) for k, v in LIMITS.items()}
        api.configure_limits({"systemOrderRps": 999})
        self.assertEqual(api.buckets["order"].rate, ORDER_RPS)
        self.assertEqual(api.buckets["order"].burst, ORDER_RPS * 2)
        api.configure_limits({"systemOrderRps": 1.5})
        self.assertEqual(api.buckets["order"].rate, 1.5)

    def test_bucket_grants_the_burst_then_paces_at_the_rate(self):
        clock = [1000.0]  # a virtual clock: sleeping advances it, nothing really waits
        bucket = TokenBucket(*LIMITS["order"])
        bucket.ts = clock[0]
        with patch.object(bingx_fast.time, "monotonic", lambda: clock[0]), \
                patch.object(bingx_fast.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            waits = [bucket.take() for _ in range(int(ORDER_RPS * 2) + 3)]
        burst = int(ORDER_RPS * 2)
        self.assertEqual(waits[:burst], [0.0] * burst)
        for wait in waits[burst:]:
            self.assertAlmostEqual(wait, 1.0 / ORDER_RPS, delta=0.02)


class BatchLane(unittest.TestCase):
    def test_batch_orders_have_their_own_bucket_and_one_ceiling(self):
        limits = json.loads((ROOT / "server/pulse/system-limits.json").read_text())
        default, low, high, _integer = limits["systemBatchRps"]
        self.assertEqual({default, high, LIMITS["batch"][0], PROFILE_BATCH_RPS}, {BATCH_RPS})
        self.assertEqual(processing_profile()["systemBatchRps"], BATCH_RPS)
        self.assertLessEqual(2 * BATCH_RPS, 0.85 * VENUE_BATCHES_PER_SECOND, "two lanes on one IP")
        self.assertEqual(FastBingX._lane(None, "/openApi/swap/v2/trade/batchOrders", "POST"), "batch")
        self.assertEqual(FastBingX._lane(None, "/openApi/swap/v2/trade/order", "POST"), "order")
        self.assertEqual(set(ORDER_LANES), {"order", "batch"}, "a rate ban on either stops both")

    def test_a_user_setting_can_lower_but_never_exceed_the_batch_ceiling(self):
        api = FastBingX.__new__(FastBingX)
        api.buckets = {k: TokenBucket(*v) for k, v in LIMITS.items()}
        api.configure_limits({"systemBatchRps": 999})
        self.assertEqual(api.buckets["batch"].rate, BATCH_RPS)
        api.configure_limits({"systemBatchRps": 1.0})
        self.assertEqual(api.buckets["batch"].rate, 1.0)
        self.assertEqual(api.buckets["order"].rate, ORDER_RPS, "the single-order bucket is untouched")

    def test_five_entries_per_call_lift_the_entry_rate_far_above_single_orders(self):
        singles = min(ORDER_RPS, REPLACE_RPS)
        batched = BATCH_RPS * 5
        self.assertGreaterEqual(batched, 3 * singles)


class EntryWindow(unittest.TestCase):
    def test_matrix_keeps_the_callers_order_only_when_asked(self):
        ranked = [(0.9, "Z-USDT", 1, "general"), (0.5, "A-USDT", 1, "general")]
        states = [f"cfg{i}" for i in range(3)]
        kept = EntryMatrix(ranked, {("general", "LONG"): states}, keep_order=True)
        self.assertEqual([kept[i][1] for i in range(2)], ["Z-USDT", "A-USDT"])
        by_name = EntryMatrix(ranked, {("general", "LONG"): states})
        self.assertEqual([by_name[i][1] for i in range(2)], ["A-USDT", "Z-USDT"])
        self.assertEqual(len(kept), len(by_name), 6)

    def test_a_ranked_desk_examines_far_more_than_eight_candidates_per_cycle(self):
        budget = LoadGovernor().observe(
            n_sym=50, n_open=10, hot_ms=20.0, warm_ms=20.0, hist_busy=False, kline_ban=False, cycle_overrun=False,
        )
        self.assertEqual(budget.level, "normal")
        self.assertEqual(budget.entry_batch, SMALL_BOOK_ENTRY_BATCH["normal"])
        self.assertGreater(budget.entry_batch, 8)
        # The time slice, not the count, is what protects SL/TP.
        self.assertLessEqual(budget.entry_budget_ms, 180.0)

    def test_critical_load_still_shrinks_the_window(self):
        self.assertLess(SMALL_BOOK_ENTRY_BATCH["critical"], SMALL_BOOK_ENTRY_BATCH["busy"])
        self.assertLess(SMALL_BOOK_ENTRY_BATCH["busy"], SMALL_BOOK_ENTRY_BATCH["normal"])


if __name__ == "__main__":
    unittest.main()
