"""1h (HTF) lane in the trader: entry routing, own controls and hold, no
Block/DCA, live-evidence crediting, queue dispatch and desk defaults."""
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
sys.path.insert(0, str(ROOT / "scripts"))
import htf_engine as he  # noqa: E402
import overall_controls  # noqa: E402
import pulse_trader as pt  # noqa: E402
import test_all_valid_entries as harness  # noqa: E402

H = harness.AllValidEntries  # its methods are borrowed below; the name is removed at the end


def htf_row(kind="z-50-2.5@x4", trail=0.0, side="LONG"):
    cfg, _ = he.preset(kind)
    if trail:
        cfg = he.ExitCfg(cfg.tp, cfg.sl, cfg.hold, trail)
    return {"id": f"htf:{kind}:{cfg.key}", "lane": "htf", "kind": kind, "symbol": "X-USDT", "direction": side,
            "slPct": cfg.sl * 100, "tpPct": cfg.tp * 100, "trailPct": cfg.trail * 100,
            "holdS": cfg.hold * 3600.0, "exitKey": cfg.key}


class HtfEntryTests(unittest.TestCase):
    setUp = H.setUp
    book = H.book
    _pulse = H.pulse

    def pulse(self):
        p = self._pulse()
        p.htf = he.HtfBook(he.HtfSettings(enabled=True))
        p.htf_sl_max = he.HTF_SL_MAX
        p.leverage_for = lambda c: 5  # auto leverage at the 15 % lane SL
        return p

    def test_entry_opens_an_own_htf_lot_with_its_wide_exits(self):
        p = self.pulse()
        p.normal_execution_enabled = False  # the lane has its own switch
        p.sets.progress.ready = False       # and does not wait for the 1m replay
        p.place("X-USDT", 1, "htf:z-50-2.5@x4", .9, htf_row(), execution_strategy="normal")
        self.assertEqual(len(p.open), 1)
        pos = next(iter(p.open.values()))
        self.assertEqual(pos.pack, "htf")
        self.assertEqual(pos.ind_kind, "z-50-2.5@x4")
        self.assertEqual(pos.ind_config, "tp10|sl5|h48")
        self.assertAlmostEqual(pos.tp_pct, 0.10)
        self.assertAlmostEqual(pos.sl_pct, 0.05)
        self.assertEqual(pos.max_hold_s, 48 * 3600.0)
        self.assertEqual(pos.exit_tactic, "", "no 1m exit tactic rides on a 1h lot")
        self.assertTrue(p.per_config_controls(pos))
        self.assertFalse(overall_controls.enabled(NS(control_orders_overall=True), pos))

    def test_trailing_preset_carries_its_trail(self):
        p = self.pulse()
        p.place("X-USDT", 1, "htf:bb-walk@x4", .9, htf_row("bb-walk@x4"), execution_strategy="normal")
        pos = next(iter(p.open.values()))
        self.assertAlmostEqual(pos.trail_arm, 0.03)
        self.assertAlmostEqual(pos.trail_give, 0.03)
        self.assertAlmostEqual(pos.sl_pct, 0.12)

    def test_same_kind_lane_is_not_reopened_while_open(self):
        p = self.pulse()
        p.place("X-USDT", 1, "htf:z-50-2.5@x4", .9, htf_row(), execution_strategy="normal")
        p.place("X-USDT", 1, "htf:z-50-2.5@x4", .9, htf_row(), execution_strategy="normal")
        self.assertEqual(len(p.open), 1)
        p.place("X-USDT", 1, "htf:break-vol@x4", .9, htf_row("break-vol@x4"), execution_strategy="normal")
        self.assertEqual(len(p.open), 2, "another kind is its own lane")

    def test_htf_lot_is_not_merged_into_the_1m_aggregate(self):
        p = self.pulse()
        p.place("X-USDT", 1, "gen:trend", .9, selected_set=p.sets.by_idx[0])
        p.place("X-USDT", 1, "htf:z-50-2.5@x4", .9, htf_row(), execution_strategy="normal")
        packs = sorted(pos.pack for pos in p.open.values())
        self.assertEqual(packs, ["general", "htf"])
        one_m = next(pos for pos in p.open.values() if pos.pack == "general")
        self.assertNotIn("htf", {getattr(m, "pack", "") for m in overall_controls.members(p, one_m)})

    def test_no_block_or_dca_lanes_for_htf(self):
        p = self.pulse()
        p.block.register_parent.reset_mock()
        p.place("X-USDT", 1, "htf:z-50-2.5@x4", .9, htf_row(), execution_strategy="normal")
        p.block.register_parent.assert_not_called()

    def test_confirmed_close_credits_the_htf_book_only_for_htf_lots(self):
        p = self.pulse()
        p.place("X-USDT", 1, "htf:z-50-2.5@x4", .9, htf_row(), execution_strategy="normal")
        pos = next(iter(p.open.values()))
        p._record_close_fill(pos, pos.qty, 110.0, "tp", exchange=True)
        self.assertEqual(len(p.htf.live.get("z-50-2.5@x4", [])), 1)
        self.assertGreater(p.htf.live["z-50-2.5@x4"][0]["r"], 0)


class HtfManageTests(unittest.TestCase):
    def manage(self, pos, px):
        for name in ("STOP_PATH", "PAUSE_PATH", "STOP_ALL", "OPEN_PATH", "LOG_PATH", "TRADES_PATH"):
            setattr(pt, name, os.path.join(tempfile.mkdtemp(), os.path.basename(getattr(pt, name))))
        p = object.__new__(pt.Pulse)
        p.ingest_ws_px = lambda: 0
        p.px = {pos.symbol: px}
        p.open = {pos.symbol: pos}
        p.control_orders = False
        p.ctrl_skip = {}
        p.exits = NS(enabled=True, ignore_tp=False, rev_on=False, min_hold_s=0,
                     decide=Mock(side_effect=AssertionError("the 1m exit engine must not judge a 1h lot")))
        p.strat_trail = True
        p.coord = NS(trailing_min_step=6)
        p.indications = NS(settings={"exitTacticOn": True})
        closes = []
        p.close_pos = lambda pos, px, why: closes.append(why)
        p.replace_sl = Mock(return_value=True)
        p.manage()
        return p, closes

    def htf_pos(self, age_h, **kw):
        base = dict(symbol="X-USDT", side="LONG", qty=1.0, entry=100.0, opened_at=time.time() - age_h * 3600,
                    sl=95.0, tp=110.0, peak=100.0, pack="htf", ind_kind="z-50-2.5@x4", set_id="htf:z",
                    max_hold_s=48 * 3600.0, exit_tactic="vwap")
        base.update(kw)
        return pt.Position(**base)

    def test_htf_lot_outlives_the_6h_desk_hold(self):
        _, closes = self.manage(self.htf_pos(7), 101.0)
        self.assertEqual(closes, [])

    def test_htf_lot_closes_at_its_own_hold(self):
        _, closes = self.manage(self.htf_pos(48.01), 101.0)
        self.assertEqual(closes, ["htf-hold-48h"])

    def test_1m_lot_still_closes_at_6h(self):
        pos = self.htf_pos(6.01, pack="general", max_hold_s=0.0, exit_tactic="")
        _, closes = self.manage(pos, 101.0)
        self.assertEqual(closes, ["max-hold-6h"])

    def test_htf_trail_arms_and_moves_the_stop_up(self):
        pos = self.htf_pos(2, peak=104.0, trail_arm=0.03, trail_give=0.03)
        p, closes = self.manage(pos, 104.0)
        self.assertEqual(closes, [])
        self.assertTrue(pos.trail_armed)
        p.replace_sl.assert_called_once()
        self.assertAlmostEqual(p.replace_sl.call_args[0][1], 104.0 * 0.97)

    def test_htf_target_closes_locally(self):
        _, closes = self.manage(self.htf_pos(1), 111.0)
        self.assertEqual(closes, ["tp"])


class HtfDispatchTests(unittest.TestCase):
    def trader(self):
        p = object.__new__(pt.Pulse)
        p.htf = he.HtfBook(he.HtfSettings(enabled=True, max_open=2))
        p._htf_queue = []
        p.halted = False
        p.open = {}
        p.entries_blocked = lambda: False
        p.errors = 0
        p.last_error = ""
        p.placed = []
        p.place = lambda sym, d, reason, conf, row, **k: p.placed.append((sym, d, reason, row["id"], k.get("execution_strategy")))
        return p

    def sig(self, kind="z-50-2.5@x4", side="LONG", sym="X-USDT", age=0.0):
        cfg, _ = he.preset(kind)
        return {"symbol": sym, "kind": kind, "side": side, "direction": 1 if side == "LONG" else -1,
                "exit": cfg, "barT": 0.0, "close": 100.0, "queuedAt": time.time() - age}

    def test_queued_signal_is_placed_once_as_its_own_lane(self):
        p = self.trader()
        p._htf_queue = [self.sig()]
        with patch.object(pt, "log"):
            p.maybe_htf_entries()
        self.assertEqual(p.placed, [("X-USDT", 1, "htf:z-50-2.5@x4", "htf:z-50-2.5@x4:tp10|sl5|h48", "normal")])
        self.assertEqual(p._htf_queue, [])

    def test_stale_and_gated_signals_are_dropped(self):
        p = self.trader()
        p.htf.s.kinds = ("rsi-mom-14-25",)
        p._htf_queue = [self.sig(age=3600), self.sig()]
        with patch.object(pt, "log"):
            p.maybe_htf_entries()
        self.assertEqual(p.placed, [])

    def test_max_open_caps_the_lane(self):
        p = self.trader()
        p.open = {f"k{i}": pt.Position(symbol="Y-USDT", side="LONG", qty=1.0, entry=1.0, opened_at=0, sl=0, tp=0,
                                       peak=0, pack="htf") for i in range(2)}
        p._htf_queue = [self.sig()]
        with patch.object(pt, "log"):
            p.maybe_htf_entries()
        self.assertEqual(p.placed, [])

    def test_disabled_lane_clears_its_queue(self):
        p = self.trader()
        p.htf.s.enabled = False
        p._htf_queue = [self.sig()]
        p.maybe_htf_entries()
        self.assertEqual((p.placed, p._htf_queue), ([], []))

    def test_history_load_never_trades_past_bars_then_new_bar_queues(self):
        p = self.trader()
        p.contracts = {"SOL-USDT": object()}
        p._htf_fetched = {}
        p._htf_busy = False
        p._htf_lock = None
        ref = json.loads((ROOT / "scripts/fixtures/htf_reference.json").read_text())["input"]
        rows = [[t, o, h, l, c, v] for t, o, h, l, c, v in zip(*[ref[k] for k in "tohlcv"])]
        calls = []

        def fetch(sym, interval, limit):
            calls.append((sym, interval, limit))
            return rows if limit > 10 else rows[-3:]
        p._fetch_klines_timed = fetch
        with patch.object(pt, "SYMBOLS", ["SOL-USDT"]):
            now = rows[-2][0] / 1000 + 3600 + 30  # the second-last bar just closed
            n = p.refresh_htf(now=now)
            self.assertEqual(n, 0, "decisions found while loading history are not entries")
            self.assertEqual(calls[0], ("SOL-USDT", "1h", p.htf.s.history_bars))
            self.assertEqual(p.htf.bars["SOL-USDT"].n, len(rows) - 1)
            p.refresh_htf(now=now + 5)
            self.assertEqual(len(calls), 1, "held the newest closed bar: no refetch")
            p.refresh_htf(now=now + 3600)
            self.assertEqual(calls[-1], ("SOL-USDT", "1h", 4))
            self.assertEqual(p.htf.bars["SOL-USDT"].n, len(rows))


class HtfDefaultsTests(unittest.TestCase):
    def test_vst_on_live_off_and_settings_load(self):
        for lane, on in (("bingx-x01", False), ("bingx-x02", True)):
            ov = json.loads((ROOT / f"server/pulse/overlay-{lane}.json").read_text())
            self.assertIs(ov["htfEnabled"], on, lane)
            s = he.HtfSettings.from_overlay(ov)
            self.assertEqual(s.enabled, on)
            self.assertEqual(set(s.kinds), set(he.HTF_KINDS))
            self.assertEqual(s.last_n, 0)

    def test_lane_sl_enters_the_auto_leverage_cap_only_when_enabled(self):
        p = object.__new__(pt.Pulse)
        p.sl_max = 0.03
        p.sl_auto_leverage = True
        p.htf_sl_max = 0.0
        self.assertEqual(p.leverage_target(150), 30)
        p.htf_sl_max = he.HTF_SL_MAX
        self.assertEqual(p.leverage_target(150), int(0.9 / he.HTF_SL_MAX))
        self.assertLessEqual(he.HTF_SL_MAX, 0.9 / p.leverage_target(150))


del H  # keep the borrowed harness out of this module's test discovery

if __name__ == "__main__":
    unittest.main()
