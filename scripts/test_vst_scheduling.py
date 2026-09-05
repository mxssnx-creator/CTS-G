"""Offline regressions from observed VST bans and long entry cycles."""
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
import bingx_fast
import pulse_trader as trader
from vst_readonly_probe import validate_base, snapshot

ORDER = "/openApi/swap/v2/trade/order"


class VstSchedulingTests(unittest.TestCase):
    def test_probe_refuses_non_vst_and_credential_bearing_urls(self):
        self.assertEqual(validate_base("https://open-api-vst.bingx.com/"), "https://open-api-vst.bingx.com")
        for base in ["https://open-api.bingx.com", "http://open-api-vst.bingx.com",
                     "https://name@open-api-vst.bingx.com", "https://open-api-vst.bingx.com/?key=test",
                     "https://open-api-vst.bingx.com/order", "https://open-api-vst.bingx.com:444"]:
            with self.assertRaises(ValueError):
                validate_base(base)

    def test_probe_uses_one_get_and_does_not_infer_ownership(self):
        calls = []
        def get(path):
            calls.append(path)
            return {"code": 0, "data": [{"symbol": "XRP-USDT", "positionAmt": "1"},
                                        {"symbol": "SOL-USDT", "positionAmt": "0"}]}
        result = snapshot(SimpleNamespace(get=get))
        self.assertEqual(calls, ["/openApi/swap/v2/user/positions"])
        self.assertEqual(result["exchangeTotalOpenCount"], 1)
        self.assertEqual(result["ownership"], "not inferred")
        self.assertFalse(snapshot(SimpleNamespace(get=lambda path: {"code": 100410, "data": []}))["ok"])

    def api(self):
        a = bingx_fast.FastBingX.__new__(bingx_fast.FastBingX)
        a.path_cd, a.cooldown_until = {}, 0
        a.stats = {"rl": 0, "wait": 0}
        a.err = SimpleNamespace(write=lambda *args, **kw: None)
        return a

    def test_both_observed_deadline_formats_and_units_are_respected(self):
        now = 1_788_598_800
        for wording in ["unblocked after ", "UNBLOCKED AFTER: ", "retry after time: "]:
            for scale in [1, 1000]:
                for delay in [80, 480, 1800]:
                    a = self.api()
                    with self.subTest(wording=wording, scale=scale, delay=delay), patch.object(bingx_fast.time, "time", return_value=now):
                        a._trip(ORDER, {"code": 109429, "msg": wording + str((now + delay) * scale)})
                        self.assertAlmostEqual(a.order_retry_after(), delay + .4, delta=1e-6)
                        self.assertGreaterEqual(a.order_retry_after(), delay)
                        self.assertEqual(a.stats["rl"], 1)

    def test_shorter_or_unspecified_deadline_cannot_shorten_existing_ban(self):
        a = self.api()
        with patch.object(bingx_fast.time, "time", return_value=1_788_598_800):
            a.path_cd[ORDER] = 1_788_599_280
            a._trip(ORDER, {"code": 100410, "msg": "rate limited"})
            self.assertEqual(a.order_retry_after(), 480)

    def test_generic_rate_limit_uses_eight_second_backoff(self):
        a = self.api()
        with patch.object(bingx_fast.time, "time", return_value=1_788_598_800):
            a._trip(ORDER, {"code": 100410, "msg": "rate limited"})
            self.assertEqual(a.order_retry_after(), 8)

    def test_rate_limit_rechecked_after_token_wait(self):
        a = self.api()
        def token_wait():
            a.path_cd[ORDER] = 200
            return .1
        a.buckets = {"order": SimpleNamespace(take=token_wait)}
        with patch.object(bingx_fast.time, "time", return_value=100):
            self.assertFalse(a._take("order", ORDER))
            self.assertEqual(a.stats["wait"], .1)

    def test_order_ban_does_not_block_private_position_snapshots(self):
        a = self.api()
        a.path_cd[ORDER], a.cooldown_until = 200, 150
        a.buckets = {"private": SimpleNamespace(take=lambda: 0)}
        with patch.object(bingx_fast.time, "time", return_value=100):
            self.assertTrue(a._take("private", "/openApi/swap/v2/user/positions"))

    def test_order_cooling_skips_entries_without_evaluating_or_posting(self):
        p = trader.Pulse.__new__(trader.Pulse)
        p.halted, p.boot_ts, p.ctrl_skip = False, 0, {}
        p.api = SimpleNamespace(order_retry_after=lambda: 400)
        p.strategy_closes = lambda: self.fail("blocked transport must not prepare entries")
        p.maybe_entries()
        p.api.order_retry_after = lambda: 0
        self.assertFalse(p.entries_blocked())

    def test_expired_slices_preserve_full_eventual_candidate_coverage(self):
        p = trader.Pulse.__new__(trader.Pulse)
        rows = list(range(100))
        clock = [0.0]
        seen = []
        with patch.object(trader.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in rows:
                for row in p.entry_candidate_window(rows):
                    seen.append(row)
                    clock[0] += 10  # Simulated slow REST call, no real sleep.
        self.assertEqual(seen, rows)
        self.assertEqual(p._entry_cursor, 0)

    def test_consumer_break_resumes_at_next_candidate(self):
        p = trader.Pulse.__new__(trader.Pulse)
        rows = list(range(6))
        self.assertEqual(next(p.entry_candidate_window(rows)), 0)
        self.assertEqual(next(p.entry_candidate_window(rows)), 1)
        self.assertEqual(list(p.entry_candidate_window([])), [])
        self.assertEqual(list(p.entry_candidate_window(rows)), [2, 3, 4, 5, 0, 1])

    def test_only_pending_empty_startup_snapshot_gets_second_read(self):
        for pending, streak, detail, expected in [
            (True, 1, "pending empty exchange read 1/2", 2),
            (False, 0, "confirmed", 1),
            (False, 1, "adopt failed", 1),
            (True, 0, "pending other", 1),
        ]:
            p = trader.Pulse.__new__(trader.Pulse)
            calls = []
            def adopt():
                calls.append("snapshot")
                p.recon_pending, p._empty_rest_streak, p.recon_detail = pending, streak, detail
            p.adopt_exchange_positions = adopt
            p.reconcile_startup_positions()
            self.assertEqual(len(calls), expected)


if __name__ == "__main__":
    unittest.main()
