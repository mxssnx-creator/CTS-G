"""1h (HTF) lane in the trader: entry routing, own controls and hold, no
Block/DCA, live-evidence crediting, queue dispatch and desk defaults."""
import json
import threading
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
        pos = self.htf_pos(6.01, pack="general", set_id="s:1", max_hold_s=0.0, exit_tactic="")
        _, closes = self.manage(pos, 101.0)
        self.assertEqual(closes, ["max-hold-6h"])

    def test_1m_lot_closes_at_the_short_trade_hold(self):
        pos = self.htf_pos(0.51, pack="general", set_id="s:1", max_hold_s=0.0, exit_tactic="")
        with patch.object(pt.Pulse, "short_max_hold_s", 1800.0, create=True):
            _, closes = self.manage(pos, 101.0)
        self.assertEqual(closes, ["max-hold-30m"])
        htf = self.htf_pos(0.6)
        with patch.object(pt.Pulse, "short_max_hold_s", 1800.0, create=True):
            _, closes = self.manage(htf, 101.0)
        self.assertEqual(closes, [], "the 1h lane keeps its own hold")

    def test_replay_hold_is_capped_to_the_short_hold(self):
        import set_engine
        book = set_engine.SetBook() if hasattr(set_engine, "SetBook") else None
        if book is None:
            self.skipTest("no SetBook")
        book.load({"setHistTimeBars": 120, "shortMaxHoldS": 1800})
        self.assertEqual(book.hist_time_bars, 30)
        book.load({"setHistTimeBars": 20, "shortMaxHoldS": 1800})
        self.assertEqual(book.hist_time_bars, 20)

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
        p._htf_lock = threading.Lock()
        p.pending_orders = {}

        def place(sym, d, reason, conf, row, **k):
            p.placed.append((sym, d, reason, row["id"], k.get("execution_strategy")))
            if not getattr(p, "refuse", False):
                p.pending_orders[f"c{len(p.placed)}"] = {"kind": "entry", "symbol": sym, "side": row["direction"],
                                                          "metadata": {"set_id": row["id"]}}
        p.place = place
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

    def test_refused_entry_stays_queued_with_backoff_until_stale(self):
        p = self.trader()
        p.refuse = True
        sig = self.sig()
        sig["closeT"] = time.time() - 60
        p._htf_queue = [sig]
        with patch.object(pt, "log"):
            p.maybe_htf_entries()
            p.maybe_htf_entries()  # inside the 60s backoff: no second order
        self.assertEqual(len(p.placed), 1)
        self.assertEqual(len(p._htf_queue), 1)
        p._htf_queue[0]["nextTry"] = 0
        p._htf_queue[0]["closeT"] = time.time() - pt.Pulse.HTF_STALE_S - 1
        with patch.object(pt, "log"):
            p.maybe_htf_entries()
        self.assertEqual((len(p.placed), p._htf_queue), (1, []), "stale from bar close: dropped")

    def test_pending_entry_blocks_a_duplicate(self):
        p = self.trader()
        p._htf_queue = [self.sig()]
        with patch.object(pt, "log"):
            p.maybe_htf_entries()
        p._htf_queue = [self.sig()]
        with patch.object(pt, "log"):
            p.maybe_htf_entries()
        self.assertEqual(len(p.placed), 1)

    def test_place_refuses_a_stop_beyond_the_lane_cap(self):
        p = object.__new__(pt.Pulse)
        p.sl_max, p.htf_sl_max, p.sl_auto_leverage = 0.03, 0.05, False
        self.assertAlmostEqual(p.sl_allowed_max(None, "htf"), 0.05)
        cfg, _ = he.preset("z-50-2.5@x4")
        self.assertLessEqual(cfg.sl, he.HTF_SL_MAX)

    def test_history_load_never_trades_past_bars_then_new_bar_queues(self):
        p = self.trader()
        p.contracts = {"SOL-USDT": object()}
        p._htf_fetched = {}
        p._htf_lock = threading.Lock()
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


def _rt(cid, pack, pnl, t, side="LONG", **kw):
    row = {"exchange_confirmed": True, "client_id": cid, "close_fill_id": cid + ":x", "conn": pt.CONN_SHORT,
           "symbol": "X-USDT", "side": side, "qty": 1.0, "entry": 100.0, "exit": 100.0 + pnl, "pnl": pnl,
           "pnl_pct": pnl / 100.0, "t": t, "hold_s": 3600.0 * 5, "pack": pack, "partial": False,
           "set_id": ("htf:z-50-2.5@x4:k" if pack == "htf" else "s:1"),
           "ind_kind": ("z-50-2.5@x4" if pack == "htf" else "rsi"), "reason": "tp"}
    row.update(kw)
    return row


class HtfEvidenceSeparationTests(unittest.TestCase):
    def trader(self, rows):
        p = object.__new__(pt.Pulse)
        p.strategy_closes = lambda: list(rows)
        return p

    def test_1m_consumers_never_see_htf_closes(self):
        rows = [_rt("a", "general", 1.0, 1.0), _rt("b", "htf", -5.0, 2.0), _rt("c", "general", -1.0, 3.0)]
        p = self.trader(rows)
        self.assertEqual([r["client_id"] for r in p.live_evidence_rows()], ["a", "c"])
        self.assertEqual([r["client_id"] for r in p.live_evidence_rows("htf")], ["b"])
        self.assertEqual(len(p.live_evidence_rows("all")), 3)
        self.assertEqual([r.client_id for r in p.live_closes()], ["a", "c"])

    def test_htf_rows_are_recognised_by_pack_set_or_lane(self):
        self.assertTrue(pt.is_htf_row({"pack": "htf"}))
        self.assertTrue(pt.is_htf_row({"set_id": "htf:rsi-mom-14-25:x"}))
        self.assertTrue(pt.is_htf_row(NS(pack="", set_id="", execution_lane="htf")))
        self.assertFalse(pt.is_htf_row({"pack": "general", "set_id": "s:1"}))

    def test_overall_parent_tape_leaves_htf_out(self):
        p = object.__new__(pt.Pulse)
        p.closed = [_rt("a", "general", 1.0, 1.0), _rt("b", "htf", -5.0, 2.0)]
        self.assertEqual([r.client_id for r in p.overall_side_closes("X-USDT", "LONG")], ["a"])

    def test_live_replaces_only_its_own_replay_twin(self):
        b = he.HtfBook(he.HtfSettings(enabled=True))
        k = "z-50-2.5@x4"

        def rows(sym):
            return [{"t": 3600.0 * (i * 4 + 3), "entry_t": 3600.0 * i * 4, "r": 0.01, "kind": k, "side": "LONG",
                     "symbol": sym, "source": "replay"} for i in range(10)]
        b.hist = {"X-USDT": {k: rows("X-USDT")}, "Y-USDT": {k: rows("Y-USDT")}}
        self.assertEqual(len(b.hist_rows(k)), 20)
        # live lot entered at 20h (+1h fill delay of the replay's 20h entry), closed at 25h
        self.assertTrue(b.on_live_close(_rt("l1", "htf", 2.0, 3600.0 * 26, hold_s=3600.0 * 5)))
        got = b.hist_rows(k)
        self.assertEqual(len([r for r in got if r["symbol"] == "X-USDT"]), 9, "only the twin is dropped")
        self.assertNotIn(3600.0 * 20, [r["entry_t"] for r in got if r["symbol"] == "X-USDT"])
        self.assertEqual(len([r for r in got if r["symbol"] == "Y-USDT"]), 10, "other symbols untouched")
        self.assertTrue(b.on_live_close(_rt("l2", "htf", 2.0, 3600.0 * 30, side="SHORT", hold_s=3600.0 * 10)))
        self.assertEqual(len(b.hist_rows(k)), 19, "other side never removes a LONG replay row")

    def test_seed_from_retained_trades_is_idempotent_and_skips_manual(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trades.jsonl")
            tag = pt.TAG
            rows = [_rt(tag + "h1", "htf", 3.0, 100.0), _rt(tag + "h2", "htf", -1.0, 200.0, reason="manual-close"),
                    _rt(tag + "g1", "general", 1.0, 300.0), _rt("foreign", "htf", 9.0, 400.0)]
            for r in rows:
                r.update(pt.SCOPE_METADATA)
            with open(path, "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in rows))
            p = object.__new__(pt.Pulse)
            p.htf = he.HtfBook(he.HtfSettings(enabled=True))
            with patch.object(pt, "TRADES_PATH", path):
                self.assertEqual(p._seed_htf_live(), 1)
                self.assertEqual(p._seed_htf_live(), 0)
            self.assertEqual([r["r"] for r in p.htf.live["z-50-2.5@x4"]], [0.03])


class HtfFetchAndConcurrencyTests(unittest.TestCase):
    def test_rate_limit_answer_stops_the_second_endpoint_and_sets_the_cooldown(self):
        p = object.__new__(pt.Pulse)
        p.kline_ban = 0.0
        p.api = Mock()
        p.api.public.return_value = {"code": 100410, "msg": "frequency limit"}
        self.assertEqual(p._fetch_klines_timed("X-USDT", "1h", 4), [])
        self.assertEqual(p.api.public.call_count, 1)
        self.assertGreater(p.kline_ban, time.time())
        self.assertEqual(p._fetch_klines_timed("X-USDT", "1h", 4), [])
        self.assertEqual(p.api.public.call_count, 1, "cooldown: no call")

    def test_readers_survive_a_concurrent_refresh(self):
        ref = json.loads((ROOT / "scripts/fixtures/htf_reference.json").read_text())["input"]
        rows = [[t, o, h, l, c, v] for t, o, h, l, c, v in zip(*[ref[k] for k in "tohlcv"])]
        b = he.HtfBook(he.HtfSettings(enabled=True))
        stop = threading.Event()
        errs = []

        def writer():
            i = 0
            while not stop.is_set():
                sym = f"S{i % 6}-USDT"
                try:
                    b.set_bars(sym, rows[: 200 + (i % 50)])
                    b.replay(sym)
                    b.on_live_close(_rt(f"w{i}", "htf", 1.0, float(i), symbol=sym))
                except Exception as exc:  # pragma: no cover - reported below
                    errs.append(exc)
                i += 1
        th = threading.Thread(target=writer)
        th.start()
        try:
            t_end = time.time() + 1.5
            while time.time() < t_end:
                b.snapshot(now_s=time.time())
                for k in b.s.kinds[:3]:
                    b.gate(k, "LONG", 1e12)
        finally:
            stop.set()
            th.join()
        self.assertEqual(errs, [])


class HtfControlPriceTests(unittest.TestCase):
    def trader(self, htf_on=True):
        p = object.__new__(pt.Pulse)
        p.sl_min, p.sl_max, p.tp_min, p.tp_max = 0.003, 0.03, 0.002, 0.03
        p.htf_sl_max = he.HTF_SL_MAX if htf_on else 0.0
        p.exits = NS(enabled=True, opt_sl_min=0.001, opt_sl_max=0.009, optimal_sl=lambda *a: 99.9)
        p.px, p.last_px, p.contracts = {"X-USDT": 101.0}, {}, {}
        return p

    def pos(self, side="LONG", **kw):
        base = dict(symbol="X-USDT", side=side, qty=1.0, entry=100.0, opened_at=time.time(), sl=0.0, tp=0.0,
                    peak=103.0 if side == "LONG" else 97.0, pack="htf", set_id="htf:z-50-2.5@x4:k", sl_pct=0.08, tp_pct=0.10)
        base.update(kw)
        return pt.Position(**base)

    def test_exchange_sl_tp_are_the_preset_distances(self):
        for on in (True, False):  # also after the lane was switched off with lots open
            p = self.trader(on)
            sl, tp = p.security_prices(self.pos())
            self.assertAlmostEqual(sl, 92.0, places=6)
            self.assertAlmostEqual(tp, 110.0, places=6)
            sl, tp = p.security_prices(self.pos("SHORT"))
            self.assertAlmostEqual(sl, 108.0, places=6)
            self.assertAlmostEqual(tp, 90.0, places=6)
            self.assertEqual(p.max_range_prices(self.pos()), p.security_prices(self.pos()))

    def test_armed_trail_is_kept_never_loosened(self):
        p = self.trader()
        sl, _ = p.security_prices(self.pos(sl=99.0, trail_armed=True))
        self.assertAlmostEqual(sl, 99.0, places=6)

    def test_1m_set_lot_keeps_its_own_range_up_to_sl_max(self):
        p = self.trader()
        lot = self.pos(pack="general", set_id="s:1", step=2, sl_pct=0.02, tp_pct=0.025, sl_ratio=0.8)
        sl_f, _, _, sl_hi = p.opt_fracs(lot)
        self.assertAlmostEqual(sl_hi, 0.03)
        self.assertAlmostEqual(sl_f, 0.02)

    def test_closed_member_never_hands_shared_ids_to_a_htf_lot(self):
        p = object.__new__(pt.Pulse)
        p.control_orders_overall = True
        p.position_is_ours = lambda x: True
        gone = pt.Position(symbol="X-USDT", side="LONG", qty=0.0, entry=1.0, opened_at=0, sl=0, tp=0, peak=0, pack="general")
        gone.sl_oid = "111"
        htf = pt.Position(symbol="X-USDT", side="LONG", qty=1.0, entry=1.0, opened_at=0, sl=0, tp=0, peak=0, pack="htf")
        p.open = {"a": gone, "b": htf}
        with patch.object(overall_controls, "cleanup_state", return_value={}) as cs, \
                patch.object(overall_controls, "save_cleanup"):
            overall_controls.closed_member(p, gone)
        self.assertEqual(list(htf.retired_control_ids), [])
        cs.assert_called_once()
        self.assertFalse(overall_controls.enabled(p, htf))
        self.assertNotIn(htf, overall_controls.members(p, gone))


class HtfRecoveryTests(unittest.TestCase):
    def test_late_pending_fill_rebuilds_the_1h_lot(self):
        p = object.__new__(pt.Pulse)
        p.variants = NS(current_sl=lambda: 0.6, current_trail=lambda: ("", 0, 0))
        p.control_orders_per_config = False  # aggregate desk: the 1h lot still stays apart
        groups = []
        p.prepare_position_group = lambda pos, legacy=False: groups.append(legacy)
        meta = {"pack": "htf", "set_id": "htf:z-50-2.5@x4:k", "sl_pct": 0.08, "tp_pct": 0.1, "max_hold_s": 48 * 3600.0,
                "ind_config": "k", "ind_kind": "z-50-2.5@x4", "trail_arm": 0.03, "trail_give": 0.02}
        row = {"symbol": "X-USDT", "side": "LONG", "client_id": pt.TAG + "e1", "created_at": 1000.0,
               "requested_qty": 2.0, "metadata": meta}
        pos = p._pending_position(row, 2.0, 100.0)
        self.assertEqual((pos.pack, pos.max_hold_s, pos.ind_config, pos.ind_kind), ("htf", 48 * 3600.0, "k", "z-50-2.5@x4"))
        self.assertAlmostEqual(pos.sl, 92.0)
        self.assertAlmostEqual(pos.tp, 110.0)
        self.assertEqual(pos.opened_at, 1000.0)
        self.assertEqual(groups, [False])


class HtfStatsTests(unittest.TestCase):
    def test_htf_closes_have_their_own_strategy_and_kind_bucket(self):
        import stats_report as sr
        import contracts
        rows = [_rt("a", "htf", 2.0, 1.0, trail_key="htf:3:2"), _rt("b", "general", 1.0, 2.0, ind_kind="trend")]
        self.assertEqual(sr._strats_of(rows[0]), ["htf"])
        self.assertNotIn("htf", sr._strats_of(rows[1]))
        strat = sr.by_strategy(rows, 0.18)
        self.assertEqual((strat["htf"]["n"], strat["trailing"]["n"]), (1, 0))
        self.assertEqual(list(sr.by_htf_kind(rows, 0.18)), ["z-50-2.5@x4"])
        self.assertEqual(sum(v["n"] for v in sr.by_indication(rows, 0.18).values()), 1)
        self.assertIn("htf", contracts.STRATEGIES)
        self.assertEqual(pt.Pulse.event_strategy(NS(**rows[0])), "htf")

    def test_sizing_snapshot_reports_min_lot_block_and_max_leverage(self):
        p = object.__new__(pt.Pulse)
        p.order_sizing, p.sl_auto_leverage = "minQty", False
        p.lev_map, p.lev_max = {"A-USDT": 50, "B-USDT": 20}, {"A-USDT": 50, "B-USDT": 75}
        p._intern_leverage_map = lambda max_map=False: dict(p.lev_max if max_map else p.lev_map)
        p.block = NS(active_increment=lambda: 0.25, extra_cap=lambda: 2.0)
        snap = p._sizing_snapshot()
        self.assertEqual((snap["orderSizing"], snap["leverageMode"], snap["pairsAtMax"], snap["pairsBelowMax"]),
                         ("minQty", "max", 1, ["B-USDT"]))
        self.assertEqual(snap["block"], {"specifiedRatio": 0.25, "effectiveRatio": 1.0, "extraCap": 2.0, "maxCounts": 2})


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
