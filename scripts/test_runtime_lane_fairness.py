"""Offline runtime lane fairness, chronology and checkpoint isolation regressions."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from history_store import HistoryStore
from load_engine import LoadGovernor
import pulse_trader as trader
from pulse_trader import Pulse, indication_config_key
from set_engine import SetBook


class RuntimeLaneFairnessTests(unittest.TestCase):
    def test_small_load_budgets_eventually_process_every_symbol(self):
        names = [f"S{i}-USDT" for i in range(12)]
        governor = LoadGovernor()
        governor.safety_partial = True
        for chunk in (1, 2, 3, 4):
            for occupied in ([], names[:1], names[:6], names):
                with self.subTest(chunk=chunk, occupied=len(occupied)):
                    cursor, seen = 0, set()
                    for _ in range(len(names) * 2):
                        window, cursor = governor.scan_window(names, occupied, chunk, cursor, names)
                        self.assertEqual(len(window), chunk)
                        self.assertEqual(len(set(window)), chunk)
                        seen.update(window)
                    self.assertEqual(seen, set(names))

    def test_repeated_symbols_do_not_consume_distinct_scan_slots(self):
        governor = LoadGovernor()
        window, _ = governor.scan_window(["A", "A", "B"], [], 3, 0)
        self.assertEqual(window, ["A", "B"])

    def test_live_klines_use_chronological_validated_candles(self):
        pulse = Pulse.__new__(Pulse)
        stamp = 1_800_000_000_000
        rows = [[stamp + i * 60_000, 100 + i, 102 + i, 99 + i, 101 + i, 5] for i in (2, 1, 0)]
        rows.extend([[stamp + 180_000, 100, 90, 101, 99, 5],
                     [stamp + 240_000, "nan", 102, 99, 101, 5]])
        self.assertEqual([row[3] for row in pulse._parse_klines(rows)], [101, 102, 103])
        dictionary = [{"time": row[0], "open": row[1], "high": row[2], "low": row[3],
                       "close": row[4], "volume": row[5]} for row in rows[:3]]
        self.assertEqual(pulse._parse_klines(dictionary), pulse._parse_klines(rows[:3]))

    def test_live_klines_deduplicate_the_same_timestamp(self):
        pulse = Pulse.__new__(Pulse)
        stamp = 1_800_000_000_000
        rows = [[stamp, 100, 102, 99, 100, 5], [stamp, 100, 102, 99, 101, 7]]
        self.assertEqual(pulse._parse_klines(rows), [[100, 102, 99, 101, 7]])

    def test_identical_closed_prices_on_new_minute_are_not_cached_forever(self):
        pulse = Pulse.__new__(Pulse)
        process = Mock()
        pulse.indications = SimpleNamespace(settings={"enabled": True}, last={"X-USDT": []}, process=process)
        pulse.open, pulse.universe = {}, []
        pulse.px = {"X-USDT": 100}
        pulse.contracts = {"X-USDT": object()}
        bars = [[100, 101, 99, 100, 5] for _ in range(40)]
        pulse.klines_tf = {"1m": {"X-USDT": bars}}
        pulse.klines = pulse.klines_tf["1m"]
        pulse.bar_min = {"X-USDT": [30_000_000, 100, 100, 100, 100]}
        pulse.tf_on, pulse._ind_fp = {"1m": True}, {}
        pulse.load = SimpleNamespace(cursor_ind=0, scan_window=lambda *args: (["X-USDT"], 0))
        pulse._budget = lambda: SimpleNamespace(scan_chunk=1, extra_sources=False, extra_n=0, tf_5m=False, tf_15m=False)
        pulse._live_scan_names = lambda: ["X-USDT"]
        pulse._drain_offline_hits = lambda: None
        pulse.score = lambda name: (1, "", .8)
        pulse.process_indications()
        pulse.process_indications()
        self.assertEqual(process.call_count, 1)
        pulse.bar_min["X-USDT"][0] += 1
        pulse.process_indications()
        self.assertEqual(process.call_count, 2)

    def test_history_and_checkpoint_refuse_a_different_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "history.json")
            checkpoint = str(pathlib.Path(directory) / "checkpoint.json")
            vst = HistoryStore("bingx-x02", path=path, checkpoint_path=checkpoint)
            vst.merge_bar("X-USDT", 100, [100, 101, 99, 100, 5], source="exchange")
            vst.checkpoint({"runId": "vst-run"})
            live = HistoryStore("bingx-x01", path=path, checkpoint_path=checkpoint)
            self.assertEqual(live.records("X-USDT"), [])
            self.assertEqual(live.checkpoint(), {})

    def test_checkpoint_payload_cannot_override_connection_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(pathlib.Path(directory) / "checkpoint.json")
            store = HistoryStore("bingx-x02", path=str(pathlib.Path(directory) / "history.json"), checkpoint_path=path)
            saved = store.checkpoint({"connection": "bingx-x01", "version": 7, "updatedAt": 0, "runId": "own"})
            self.assertEqual(saved["connection"], "bingx-x02")
            self.assertEqual(saved["version"], 1)
            self.assertGreater(saved["updatedAt"], 0)
            self.assertEqual(store.checkpoint()["runId"], "own")

    def test_durable_replay_keeps_actual_closed_minute_for_timeframe_buckets(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore("bingx-x02", path=str(pathlib.Path(directory) / "history.json"))
            first = 30_000_000
            last = first + 179
            store.merge("X", [{"minute": minute, "bar": [100, 101, 99, 100, 5]}
                              for minute in range(first, last + 1)], source="exchange", persist=False)
            pulse = Pulse.__new__(Pulse)
            pulse.history_store = store
            book = SetBook()
            coverage, missing = pulse._hist_coverage(book, ["X"], first, last + 3)
            self.assertFalse(missing)
            self.assertEqual(coverage["X"]["watermark"], last)
            self.assertEqual(book._bar_end_ts["X"], last * 60)
            clone = book.replay_clone(["X"])
            self.assertEqual(clone._bar_end_ts["X"], last * 60)

    def test_timestamped_replay_transport_survives_slicing_and_process_pickle(self):
        import hist_calc
        import pickle
        rows = [[100, 101, 99, 100, 5] for _ in range(3)]
        stamps = [1_800_000_000_000 + i * 60_000 for i in range(3)]
        bars = hist_calc.TimedReplayBars(rows, stamps)
        restored = pickle.loads(pickle.dumps(bars))
        self.assertEqual(restored, rows)
        self.assertEqual(restored.timestamps, tuple(stamps))
        self.assertEqual(restored[:2].end_timestamp, stamps[1] / 1000)
        book = SetBook()
        hist_calc._install_worker_bars(book, "X", restored)
        self.assertEqual(book._bar_end_ts["X"], stamps[-1] / 1000)
        hist_calc._install_worker_bars(book, "X", rows)
        self.assertNotIn("X", book._bar_end_ts, "a reused process cannot borrow an earlier input's time")

    def test_historic_public_fetch_drops_forming_candle_and_preserves_actual_time(self):
        import hist_calc
        from urllib.parse import parse_qs, urlparse
        now = 1_800_000_039
        current = now // 60 * 60_000
        def public(url, **kwargs):
            request = parse_qs(urlparse(url).query)
            start, end = int(request["startTime"][0]), int(request["endTime"][0])
            return {"data": [[stamp, 100, 101, 99, 100, 5] for stamp in range(start, end + 1, 60_000)][::-1]}
        with patch.object(hist_calc.time, "time", return_value=now), patch.object(hist_calc, "_public_json", side_effect=public):
            bars = hist_calc.fetch_klines("X", 60)
        self.assertEqual(len(bars), 60)
        self.assertEqual(bars.end_timestamp, (current - 60_000) / 1000)
        self.assertTrue(all(stamp < current for stamp in bars.timestamps))

    def test_entry_matrix_selects_each_ranges_own_exit_sets(self):
        pulse = Pulse.__new__(Pulse)
        winning = [SimpleNamespace(id="range-A-tp3"), SimpleNamespace(id="range-A-tp4")]
        losing = []
        api = Mock(side_effect=lambda kind, config, side, symbol: winning if config == "trend:14" and side == "LONG" else losing)
        pulse.sets = SimpleNamespace(enabled=True, indication_entry_sets=api)
        pulse.indications = SimpleNamespace(match=lambda symbol, reason: SimpleNamespace(
            kind="trend", mode="trend:14" if "good" in reason else "trend:28"))
        signals = [(.9, "A", 1, "ind:trend:good"), (.8, "B", 1, "ind:trend:good"), (.7, "C", 1, "ind:trend:bad")]
        pooled = SimpleNamespace(id="pooled-unrelated")
        matrix = pulse.build_entry_matrix(signals, {("indications", "LONG"): [pooled]})
        self.assertEqual(len(matrix), 4)
        rows = [matrix[index] for index in range(len(matrix))]
        self.assertEqual({row[1] for row in rows}, {"A", "B"})
        self.assertEqual({row[-1].id for row in rows}, {"range-A-tp3", "range-A-tp4"})
        self.assertEqual(api.call_count, 3, "each symbol owns its own configuration verdict")

    def test_disappeared_exact_signal_has_no_pooled_fallback(self):
        pulse = Pulse.__new__(Pulse)
        api = Mock(return_value=[SimpleNamespace(id="unrelated")])
        pulse.sets = SimpleNamespace(enabled=True, indication_entry_sets=api)
        pulse.indications = SimpleNamespace(match=lambda *args: None)
        matrix = pulse.build_entry_matrix([(.9, "X", 1, "ind:signals:direct_tf cfg=gone")],
                                          {("indications", "LONG"): [SimpleNamespace(id="pooled")]})
        self.assertEqual(len(matrix), 0)
        api.assert_not_called()

    def test_signals_timeframes_keep_different_configurations(self):
        self.assertEqual(indication_config_key(SimpleNamespace(mode="direct_tf", timeframe="1m")), "direct_tf:1m")
        self.assertEqual(indication_config_key(SimpleNamespace(mode="direct_tf", timeframe="5m")), "direct_tf:5m")
        self.assertEqual(indication_config_key(SimpleNamespace(mode="tf_combined", timeframe="multi")), "tf_combined")
        self.assertEqual(indication_config_key(SimpleNamespace(kind="tactic", mode="tactic:reversion:14", timeframe="30m")), "tactic:reversion:14:30m")
        self.assertEqual(indication_config_key(SimpleNamespace(kind="tactic", mode="tactic:reversion:14:combined", timeframe="combined")), "tactic:reversion:14:combined")

    def test_selected_exit_set_cannot_be_used_for_a_different_indication_range(self):
        pulse = Pulse.__new__(Pulse)
        pulse.px, pulse.strat_ind = {"X": 100}, True
        pulse.execution_lane_key = lambda *args: "lane"
        pulse.occupying = lambda *args, **kwargs: False
        pulse.sets = SimpleNamespace(enabled=True, use_historic_gate=False,
                                     indication_entry_sets=lambda *args: [], execution_allowed=lambda *args: True)
        pulse.indications = SimpleNamespace(settings={"enabled": True},
                                           match=lambda *args: SimpleNamespace(kind="signals", mode="direct_tf", timeframe="5m", direction="long"))
        wrong = SimpleNamespace(indication_kind="signals", indication_config="direct_tf:1m", indication_symbol="X")
        own = SimpleNamespace(indication_kind="signals", indication_config="direct_tf:5m", indication_symbol="X")
        other_symbol = SimpleNamespace(indication_kind="signals", indication_config="direct_tf:5m", indication_symbol="Y")
        self.assertEqual(pulse.entry_sense("X", 1, "ind:signals:direct_tf", .8, "indications", wrong), "ind-set-mismatch")
        self.assertIsNone(pulse.entry_sense("X", 1, "ind:signals:direct_tf", .8, "indications", own))
        self.assertEqual(pulse.entry_sense("X", 1, "ind:signals:direct_tf", .8, "indications", other_symbol), "set-symbol-mismatch")

    def test_forced_live_gate_uses_return_pf_even_when_all_trades_win(self):
        pulse = Pulse.__new__(Pulse)
        pulse.api = SimpleNamespace(base="https://open-api-vst.bingx.com")
        pulse.sets = SimpleNamespace(live_test_mode=True)
        pulse.overlay, pulse.open, pulse.coord = {"baseEvalPosCount": 30}, {}, SimpleNamespace(min_pf=1.1)
        pulse.position_cost_pct = .1
        candidate = {"id": "forced-own", "symbol": "X", "direction": "LONG"}
        pulse._forced_data = lambda: {"rows": [candidate]}
        pulse.closed = [SimpleNamespace(set_id="forced-own")]
        weak = [{"pnl_pct": .0011, "t": 1, "side": "LONG"}]
        strong = [{"pnl_pct": .003, "t": 1, "side": "LONG"}]
        with patch.object(trader, "CONN_SHORT", "bingx-x02"), patch.object(trader, "valid_candidate", return_value=True), \
                patch.object(trader, "completed_roundtrips", return_value=weak):
            self.assertFalse(pulse._forced_entry_allowed(candidate, "X", "LONG", .8))
        with patch.object(trader, "CONN_SHORT", "bingx-x02"), patch.object(trader, "valid_candidate", return_value=True), \
                patch.object(trader, "completed_roundtrips", return_value=strong):
            self.assertTrue(pulse._forced_entry_allowed(candidate, "X", "LONG", .8))

    def test_exact_signals_timeframe_reaches_confirmed_fill_and_pending_intent(self):
        import test_all_valid_entries as base
        helper = base.AllValidEntries("setUp")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        book = helper.book(1)
        own = book.by_idx[0]
        own.pack, own.indication_kind, own.indication_config = "indications", "signals", "direct_tf:5m"
        own.indication_symbol = "X-USDT"
        book._reindex()
        book._indication_grid = SimpleNamespace(current=lambda state: state is own)
        pulse = helper.pulse(book)
        indication = helper.indication()
        indication.kind, indication.mode, indication.timeframe = "signals", "direct_tf", "5m"
        pulse.indications.last["X-USDT"] = [indication]
        reason = f"ind:signals:direct_tf cfg={indication.entry_key}"
        pulse.place("X-USDT", 1, reason, .8, selected_set=own)
        self.assertEqual(len(pulse.open), 1, pulse.last_error)
        position = next(iter(pulse.open.values()))
        self.assertEqual(position.ind_config, "direct_tf:5m")
        self.assertEqual(position.set_id, own.id)
        pulse.open.clear()
        pulse.cooldown.clear()
        pulse.last_entry_ts = 0
        pulse.api.fill_fraction = 0
        pulse.place("X-USDT", 1, reason, .8, selected_set=own)
        self.assertEqual(len(pulse.open), 0)
        pending = next(iter(pulse.pending_orders.values()))
        self.assertEqual(pending["metadata"]["ind_config"], "direct_tf:5m")
        self.assertEqual(pending["metadata"]["set_id"], own.id)

    def test_confirmed_add_and_partial_closes_keep_parent_and_child_accounting_separate(self):
        import test_all_valid_entries as base
        from dataclasses import replace
        from dca_lot_ledger import DcaLotLedger
        helper = base.AllValidEntries("setUp")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        book = helper.book(1)
        parent = book.by_idx[0]
        parent.indication_config, parent.indication_symbol = "general_signal", "X-USDT"
        book._indication_grid = SimpleNamespace(current=lambda state: True)
        pulse = helper.pulse(book)
        pulse.place("X-USDT", 1, "gen:trend", .8, selected_set=parent)
        position = next(iter(pulse.open.values()))
        child = replace(parent, id=parent.id + ":adjust:dca:1", kind="dca", parent_set_id=parent.id,
                        axis_key="dca:1", relative_count=1, live=[])
        book.sets[child.id] = child
        book.by_idx.append(child)
        pulse._dca_lot_ledger = DcaLotLedger(trader.CONN_SHORT, path=str(pathlib.Path(helper.tmp.name) / "dca-lots.json"))
        pulse.pending_orders["dca-fill"] = {"metadata": {"dca_child_set_id": child.id, "axis_key": "dca:1", "relative_count": 1,
                                                       "parent_set_id": parent.id, "ind_config": "general_signal"}}
        pulse.control_orders = False
        pulse._apply_position_fill(position, .05, 90, source="dca", fill_cid="dca-fill", order_id="dca-order")
        self.assertAlmostEqual(position.qty, .1)
        self.assertAlmostEqual(position.entry, 95)
        original_qty = position.qty
        self.assertTrue(pulse._record_close_fill(position, .05, 101, "tp", close_cid="close-half", cumulative_qty=.05))
        self.assertEqual(len(pulse.closed), 2)
        self.assertTrue(all(row.partial for row in pulse.closed))
        self.assertEqual({row.set_id for row in pulse.closed}, {parent.id, child.id})
        self.assertEqual({row.entry for row in pulse.closed}, {100, 90})
        self.assertAlmostEqual(sum(row.qty for row in pulse.closed), .05)
        self.assertTrue(pulse._record_close_fill(position, .05, 101, "tp", close_cid="close-final", cumulative_qty=.05))
        self.assertEqual(len(pulse.closed), 4)
        self.assertAlmostEqual(sum(row.qty for row in pulse.closed), original_qty)
        expected = trader.net_pnl_usdt((101 - 95) / 95, .1, 95, pulse.position_cost_pct)
        self.assertAlmostEqual(sum(row.pnl for row in pulse.closed), expected)
        self.assertEqual(len(parent.evaluation_live()), 1)
        self.assertEqual(len(child.evaluation_live()), 1)
        self.assertEqual(child.evaluation_live()[0]["strategy"], "dca")

    def test_axis_lot_preserves_own_identity_trailing_and_relative_count_ratio(self):
        import test_all_valid_entries as base
        from dataclasses import replace
        helper = base.AllValidEntries("setUp")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        book = helper.book(1)
        parent = helper._real_qualify(book.by_idx[0])
        parent.indication_config, parent.indication_symbol = "general_signal", "X-USDT"
        child = replace(parent, id="axis-own", kind="axis", parent_set_id=parent.id, axis_key="last:4",
                        relative_count=4, volume_ratio=.04, trail_key="0.3:0.1", trail_arm=.3, trail_give=.1)
        book.sets[child.id] = child
        book.by_idx.append(child)
        book._indication_grid = SimpleNamespace(current=lambda state: True)
        pulse = helper.pulse(book)
        pulse.strat_trail = True
        pulse.size_qty = Mock(return_value=.05)
        pulse.place("X-USDT", 1, "gen:trend", .8, selected_set=child)
        self.assertEqual(len(pulse.open), 1, pulse.last_error)
        position = next(iter(pulse.open.values()))
        self.assertEqual(position.strategy, "axis")
        self.assertEqual(position.set_id, child.id)
        self.assertEqual(position.parent_set_id, parent.id)
        self.assertEqual(position.axis_key, "last:4")
        self.assertEqual(position.trail_key, "0.3:0.1")
        self.assertAlmostEqual(position.volume_ratio, .04)
        self.assertAlmostEqual(pulse.size_qty.call_args.kwargs["ratio"], .04)

    def test_block_child_uses_its_own_execution_lane_and_parent_identity(self):
        import test_all_valid_entries as base
        from dataclasses import replace
        from block_engine import BlockBook
        helper = base.AllValidEntries("setUp")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        book = helper.book(1)
        parent = helper._real_qualify(book.by_idx[0])
        parent.indication_config, parent.indication_symbol = "general_signal", "X-USDT"
        child = helper._real_qualify(replace(parent, id=parent.id + ":adjust:block:2", kind="block", parent_set_id=parent.id,
                                            axis_key="block-active:2", relative_count=2, volume_ratio=.25))
        book.sets[child.id] = child
        book.by_idx.append(child)
        book._indication_grid = SimpleNamespace(current=lambda state: True)
        pulse = helper.pulse(book)
        pulse.block = BlockBook("", {})
        pulse.block_active, pulse.strat_block, pulse.recon_ok = True, True, True
        pulse.coord.min_pf = 1.1
        pulse._coord_add_state = lambda **kwargs: (True, 6, 1.8, [])
        execution = pulse.execution_lane_key("general", "gen:trend", child, "block-active")
        pulse._block_reference_anchors = {("X-USDT", "LONG", execution):
            {"at": trader.time.time() - 60, "seen": trader.time.time(), "price": 99.5, "direction": 1}}
        pulse.place("X-USDT", 1, "gen:trend", .8, selected_set=child)
        self.assertEqual(len(pulse.open), 1, pulse.last_error)
        position = next(iter(pulse.open.values()))
        self.assertEqual(position.strategy, "block")
        self.assertEqual(position.set_id, child.id)
        self.assertEqual(position.parent_set_id, parent.id)
        self.assertEqual(position.axis_key, "block-active:2")
        self.assertEqual(position.execution_lane, execution)
        self.assertEqual(pulse._entry_modes(None, parent), ["normal"])
        addon = replace(child, axis_key="block-add:2")
        self.assertEqual(pulse._entry_modes(None, addon), [])

    def test_general_candidate_uses_the_symbols_own_evidence(self):
        pulse = Pulse.__new__(Pulse)
        own = SimpleNamespace(id="A-own")
        api = Mock(side_effect=lambda pack, side, symbol: [own] if symbol == "A" else [])
        pulse.sets = SimpleNamespace(enabled=True, pack_entry_sets=api)
        matrix = pulse.build_entry_matrix([(.9, "A", 1, "gen:trend"), (.8, "B", 1, "gen:trend")],
                                          {("general", "LONG"): [SimpleNamespace(id="pooled-other")]})
        self.assertEqual(len(matrix), 1)
        self.assertEqual(matrix[0][1], "A")
        self.assertEqual(matrix[0][-1].id, "A-own")


if __name__ == "__main__":
    unittest.main()
