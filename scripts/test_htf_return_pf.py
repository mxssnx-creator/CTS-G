"""No-network regressions for HTF return-PF admission and bar cache freshness."""
import pathlib
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
import htf_engine as he
from position_cost import net_return_pf

KIND = "rsi-mom-14-25"
OTHER_KIND = "rsi-mom-21-25"
NOW = 1_000_000.0


def evidence(kind, returns, side="LONG"):
    return [{"t": NOW - len(returns) + i, "r": r, "kind": kind,
             "side": side, "symbol": "X-USDT", "source": "replay"}
            for i, r in enumerate(returns)]


def low_return_classic99():
    # A very high win/loss ratio still earns much less than one PositionCost.
    return [0.00001] * 19 + [-0.00019 / 99.0]


class ReturnPfAdmission(unittest.TestCase):
    def book(self, **settings):
        defaults = dict(enabled=True, kinds=(KIND, OTHER_KIND), min_n=20,
                        side_min_trades=1000, cost=0.0018)
        defaults.update(settings)
        return he.HtfBook(he.HtfSettings(**defaults))

    def test_already_net_returns_are_not_charged_twice(self):
        self.assertAlmostEqual(he.pf_cost([0.0018] * 20, 0.0018), 1.10)
        self.assertAlmostEqual(he.pf_cost([-0.0018] * 20, 0.0018), 0.90)
        self.assertAlmostEqual(he.pf_cost([0.0] * 20, 0.0018), 1.0)
        self.assertAlmostEqual(he.pf_cost(np.array([0.0018]), 0.0018), 1.10)
        self.assertAlmostEqual(he.pf_cost([0.001], 0), net_return_pf(0.001, 0))

    def test_classic99_cannot_admit_a_low_return_kind(self):
        b = self.book()
        returns = low_return_classic99()
        self.assertAlmostEqual(he.pf_classic(returns), 99.0)
        b.hist["X-USDT"] = {KIND: evidence(KIND, returns),
                              OTHER_KIND: evidence(OTHER_KIND, [b.s.cost] * 20)}
        ok, why, info = b.gate(KIND, "LONG", NOW)
        self.assertFalse(ok, why)
        self.assertLess(info["pf"], b.s.min_pf)
        self.assertEqual(info["classicPf"], 99.0)
        self.assertTrue(b.gate(OTHER_KIND, "LONG", NOW)[0], "kind evidence is independent")

    def test_family_direction_gate_uses_return_pf(self):
        b = self.book(min_n=1000, side_min_trades=20)
        b.hist["X-USDT"] = {KIND: evidence(KIND, low_return_classic99())}
        ok, why, info = b.gate(OTHER_KIND, "LONG", NOW)
        self.assertFalse(ok, why)
        self.assertEqual(info["sideAccept"]["classicPf"], 99.0)
        self.assertLess(info["sideAccept"]["pf"], b.s.side_min_pf)
        self.assertTrue(b.gate(OTHER_KIND, "SHORT", NOW)[0], "direction evidence is independent")

    def test_snapshot_headlines_and_live_headlines_use_net_return(self):
        b = self.book()
        self.assertTrue(b.on_live_close({"exchange_confirmed": True,
            "ind_kind": KIND, "close_fill_id": "fill-1", "client_id": "client-1",
            "t": NOW, "side": "LONG", "symbol": "X-USDT",
            "entry": 100.0, "qty": 1.0, "pnl": 0.18}))
        row = b.snapshot(NOW)["kinds"][KIND]
        self.assertAlmostEqual(row["pf"], 1.10)
        self.assertAlmostEqual(row["livePf"], 1.10)
        self.assertEqual(row["classicPf"], he.pf_classic([0.0018]))
        self.assertEqual(row["liveClassicPf"], he.pf_classic([0.0018]))

    def test_empty_evidence_is_explicitly_absent_in_stats(self):
        b = self.book()
        row = b.snapshot(NOW)["kinds"][KIND]
        for metric in ("pf", "livePf", "classicPf", "liveClassicPf"):
            self.assertIsNone(row[metric], metric)
        _, _, info = b.gate(KIND, "LONG", NOW)
        self.assertIsNone(info["pf"])
        self.assertIsNone(info["sideAccept"]["pf"])

    def test_snapshot_refreshes_after_in_place_settings_change(self):
        b = self.book()
        b.hist["X-USDT"] = {KIND: evidence(KIND, [b.s.cost] * 20)}
        self.assertTrue(b.snapshot(NOW)["kinds"][KIND]["long"]["ok"])
        b.s.min_pf = 1.20
        self.assertFalse(b.snapshot(NOW)["kinds"][KIND]["long"]["ok"])

    def test_snapshot_refreshes_when_live_tape_is_already_capped(self):
        b = self.book(window=5, min_n=5)
        def close(i, pnl):
            return b.on_live_close({"exchange_confirmed": True, "ind_kind": KIND,
                "close_fill_id": f"fill-{i}", "t": NOW - 501 + i,
                "side": "LONG", "symbol": "X-USDT", "entry": 100.0,
                "qty": 1.0, "pnl": pnl})
        for i in range(500):
            self.assertTrue(close(i, 0.18))
        before = b.snapshot(NOW)["kinds"][KIND]
        self.assertTrue(before["long"]["ok"])
        self.assertTrue(close(500, -10.0))
        self.assertEqual(len(b.live[KIND]), 500)
        after = b.snapshot(NOW)["kinds"][KIND]
        self.assertFalse(after["long"]["ok"])
        self.assertLess(after["pf"], before["pf"])
        self.assertLess(after["livePf"], before["livePf"])


class BarSnapshotFreshness(unittest.TestCase):
    def test_same_timestamp_correction_refreshes_signals_without_duplicate_entries(self):
        b = he.HtfBook(he.HtfSettings(enabled=True, kinds=(KIND,)))
        rows = [[i * he.HOUR_MS, 100.0, 102.0, 98.0, 100.0, 1000.0] for i in range(60)]
        b.set_bars("X-USDT", rows)

        def signals(bars, kinds, **kwargs):
            sig = np.where(bars.c > 100.0, 1, -1).astype(np.int8)
            return {k: sig for k in kinds}

        with patch.object(he, "entry_signals", side_effect=signals) as calc:
            old = b.signals("X-USDT")
            self.assertEqual(int(old[KIND][-1]), -1)
            self.assertIs(b.signals("X-USDT"), old)
            b.new_signals("X-USDT")
            unchanged = b.bars["X-USDT"]
            b.set_bars("X-USDT", rows)
            self.assertIs(b.bars["X-USDT"], unchanged)
            self.assertIs(b.signals("X-USDT"), old)
            self.assertEqual(calc.call_count, 1)

            corrected = list(rows[-1])
            corrected[4] = 101.0
            b.set_bars("X-USDT", [corrected])
            self.assertEqual(b.bars["X-USDT"].t[-1], unchanged.t[-1])
            fresh = b.signals("X-USDT")
            self.assertIsNot(fresh, old)
            self.assertEqual(int(fresh[KIND][-1]), 1)
            self.assertEqual(calc.call_count, 2)
            self.assertEqual(b.new_signals("X-USDT"), [], "already decided bar cannot execute twice")


class OwnSymbolDirectionAdmission(unittest.TestCase):
    def book(self):
        return he.HtfBook(he.HtfSettings(enabled=True, kinds=(KIND,), min_n=20,
            window=50, side_min_trades=1000, cost=0.0018))

    def test_neighboring_winners_cannot_admit_an_own_loser_or_cold_symbol(self):
        b = self.book()
        b.hist["X-USDT"] = {KIND: evidence(KIND, [-b.s.cost] * 20)}
        neighbor = [{**r, "symbol": "Y-USDT"} for r in evidence(KIND, [10 * b.s.cost] * 50)]
        b.hist["Y-USDT"] = {KIND: neighbor}
        self.assertTrue(b.gate(KIND, "LONG", NOW)[0], "pooled headline remains diagnostic")
        allowed, why, info = b.gate(KIND, "LONG", NOW, symbol="X-USDT")
        self.assertFalse(allowed, why)
        self.assertEqual(info["n"], 20)
        self.assertAlmostEqual(info["pf"], 0.9)
        cold, why, info = b.gate(KIND, "LONG", NOW, symbol="COLD-USDT")
        self.assertFalse(cold, why)
        self.assertEqual(info["n"], 0)
        self.assertIsNone(info["pf"])
        self.assertIn("sample 0/20", why)

    def test_own_long_winners_cannot_admit_short(self):
        b = self.book()
        b.hist["X-USDT"] = {KIND: evidence(KIND, [b.s.cost] * 20)}
        self.assertTrue(b.gate(KIND, "LONG", NOW, symbol="X-USDT")[0])
        short, why, info = b.gate(KIND, "SHORT", NOW, symbol="X-USDT")
        self.assertFalse(short, why)
        self.assertEqual(info["n"], 0)
        self.assertEqual(info["scope"], "symbol-direction")

    def test_configured_minimum_sample_floor_is_exact(self):
        b = self.book()
        rows = evidence(KIND, [b.s.cost] * 20)
        b.hist["X-USDT"] = {KIND: rows[:-1]}
        allowed, why, info = b.gate(KIND, "LONG", NOW, symbol="X-USDT")
        self.assertFalse(allowed, why)
        self.assertEqual((info["n"], info["requiredSamples"]), (19, 20))
        b.hist["X-USDT"][KIND] = rows
        self.assertTrue(b.gate(KIND, "LONG", NOW, symbol="X-USDT")[0])

    def test_scope_filter_precedes_live_first_window(self):
        b = self.book()
        b.hist["X-USDT"] = {KIND: evidence(KIND, [b.s.cost] * 20)}
        b.live[KIND] = [{**r, "symbol": "Y-USDT", "source": "live"}
                        for r in evidence(KIND, [-b.s.cost] * 50)]
        own, source = b.evidence(KIND, "X-USDT", "LONG")
        self.assertEqual(len(own), 20)
        self.assertTrue(all(r["symbol"] == "X-USDT" and r["side"] == "LONG" for r in own))
        allowed, why, info = b.gate(KIND, "LONG", NOW, symbol="X-USDT")
        self.assertTrue(allowed, why)
        self.assertEqual(info["liveN"], 0)

    def test_future_own_closes_cannot_qualify_a_historical_entry(self):
        b = self.book()
        b.hist["X-USDT"] = {KIND: [{**r, "t": NOW + 1 + i}
            for i, r in enumerate(evidence(KIND, [b.s.cost] * 20))]}
        allowed, why, info = b.gate(KIND, "LONG", NOW, symbol="X-USDT")
        self.assertFalse(allowed, why)
        self.assertEqual(info["n"], 0)


if __name__ == "__main__":
    unittest.main()
