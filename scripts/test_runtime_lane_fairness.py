"""Offline runtime lane fairness, chronology and checkpoint isolation regressions."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from history_store import HistoryStore
from load_engine import LoadGovernor
import pulse_trader as trader
from pulse_trader import Pulse, indication_config_key
from set_engine import SetBook


class RuntimeLaneFairnessTests(unittest.TestCase):
    def partitioned_fixture(self, *, free_entry=False, child_fee=0., child_fee_known=False, preclose=False):
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
        position.entry_fee_known = free_entry
        if preclose:
            pulse.control_orders = False
            self.assertTrue(pulse._record_close_fill(position, .025, 101, "tp", close_cid="before-dca-close",
                                                   cumulative_qty=.025, exit_fee_known=free_entry))
        child = replace(parent, id=parent.id + ":adjust:dca:1", kind="dca", parent_set_id=parent.id,
                        axis_key="dca:1", relative_count=1, live=[])
        book.sets[child.id] = child
        book.by_idx.append(child)
        path = str(pathlib.Path(helper.tmp.name) / "dca-lots.json")
        pulse._dca_lot_ledger = DcaLotLedger(trader.CONN_SHORT, path=path)
        child_cid = pulse.cid("d", position)
        pulse.pending_orders[child_cid] = {"metadata": {"dca_child_set_id": child.id, "axis_key": "dca:1",
                                                         "relative_count": 1, "parent_set_id": parent.id,
                                                         "ind_config": "general_signal"}}
        pulse.control_orders = False
        pulse._apply_position_fill(position, .05, 90, source="dca", fill_cid=child_cid, order_id="dca-order",
                                   fee=child_fee, fee_known=child_fee_known)
        return pulse, position, parent, child, path

    def test_late_normal_partial_fill_after_dca_conserves_own_inventory_and_basis(self):
        pulse, position, parent, child, _ = self.partitioned_fixture()
        pulse._apply_position_fill(position, .05, 110, source="entry", fill_cid=position.client_id,
                                   order_id=position.order_id, fill_sequence=.1)
        self.assertAlmostEqual(position.qty, .15)
        self.assertAlmostEqual(position.entry, 100)
        self.assertAlmostEqual(pulse._dca_lot_ledger.parent_qty(position.client_id), .15)
        self.assertTrue(pulse._record_close_fill(position, .15, 105, "tp", close_cid="late-core-close", cumulative_qty=.15))
        rows = {row.set_id: row for row in pulse.closed}
        self.assertAlmostEqual(rows[parent.id].entry, 105)
        self.assertAlmostEqual(rows[parent.id].qty, .1)
        self.assertAlmostEqual(rows[child.id].entry, 90)
        self.assertAlmostEqual(rows[child.id].qty, .05)
        self.assertAlmostEqual(sum(row.pnl for row in rows.values()), trader.net_pnl_usdt(.05, .15, 100, pulse.position_cost_pct))

    def test_measured_free_commissions_remain_zero_on_each_independent_lot(self):
        pulse, position, _, _, _ = self.partitioned_fixture(free_entry=True, child_fee_known=True)
        self.assertTrue(pulse._record_close_fill(position, .1, 101, "tp", exit_fee_known=True))
        self.assertAlmostEqual(sum(row.pnl for row in pulse.closed), .6)
        self.assertTrue(all(row.position_cost_pct == 0 and row.cost_source == "live-exchange" for row in pulse.closed))
        self.assertTrue(trader.exchange_fee_known({"commission": "0"}))
        self.assertFalse(trader.exchange_fee_known({"avgPrice": "101"}))

    def test_measured_child_fees_are_not_replaced_by_unknown_parent_fallback(self):
        pulse, position, parent, child, _ = self.partitioned_fixture(child_fee=.002, child_fee_known=True)
        self.assertTrue(pulse._record_close_fill(position, .1, 101, "tp", exit_fee=.003, exit_fee_known=True))
        rows = {row.set_id: row for row in pulse.closed}
        self.assertAlmostEqual(rows[parent.id].position_cost_pct, pulse.position_cost_pct)
        self.assertEqual(rows[child.id].cost_source, "live-exchange")
        self.assertAlmostEqual(rows[child.id].position_cost_pct, .0035 / 4.5 * 100)
        self.assertAlmostEqual(rows[child.id].pnl, .55 - .0035)

    def test_acknowledged_receipt_repairs_stale_physical_book_after_restart(self):
        from dataclasses import replace
        from dca_lot_ledger import DcaLotLedger
        pulse, position, _, _, path = self.partitioned_fixture()
        stale = replace(position)
        self.assertTrue(pulse._record_close_fill(position, .1, 101, "tp", close_cid="full-close", cumulative_qty=.1))
        self.assertEqual(pulse._dca_lot_ledger.pending_closes(), [])
        pulse.open = {pulse.position_key(stale): stale}
        pulse.pending_orders["full-close"] = {"kind": "close", "requested_qty": .1, "filled_qty": 0}
        pulse._dca_lot_ledger = DcaLotLedger(trader.CONN_SHORT, path=path)
        pulse._restore_independent_close_state()
        self.assertEqual(pulse.open, {})
        self.assertNotIn("full-close", pulse.pending_orders)

    def test_cumulative_entry_average_yields_the_actual_incremental_fill_price(self):
        import test_all_valid_entries as base
        helper = base.AllValidEntries("setUp")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        pulse = helper.pulse(helper.book(1))
        pulse.api.fill_fraction = .5
        pulse.place("X-USDT", 1, "gen:trend", .8, selected_set=pulse.sets.by_idx[0])
        position = next(iter(pulse.open.values()))
        cid = position.client_id
        track = pulse.parse_track(cid)
        self.assertTrue(pulse._sync_pending_fill({"symbol": position.symbol, "positionSide": "LONG",
                                                "orderId": position.order_id, "executedQty": .05,
                                                "avgPrice": 95, "status": "FILLED", "commission": "0"}, cid, track, "o"))
        self.assertAlmostEqual(position.qty, .05)
        self.assertAlmostEqual(position.entry, 95)

    def test_partial_acknowledged_receipt_restores_quantity_fees_and_pending_marker(self):
        from dataclasses import replace
        from dca_lot_ledger import DcaLotLedger
        pulse, position, _, _, path = self.partitioned_fixture(child_fee=.002, child_fee_known=True)
        stale = replace(position)
        self.assertTrue(pulse._record_close_fill(position, .05, 101, "tp", close_cid="partial-close", cumulative_qty=.05))
        pulse.open = {pulse.position_key(stale): stale}
        pulse.pending_orders["partial-close"] = {"kind": "close", "requested_qty": .1, "filled_qty": 0}
        pulse._dca_lot_ledger = DcaLotLedger(trader.CONN_SHORT, path=path)
        pulse._restore_independent_close_state()
        self.assertAlmostEqual(stale.qty, .05)
        self.assertAlmostEqual(stale.entry, 95)
        self.assertAlmostEqual(stale.entry_fee, .001)
        self.assertAlmostEqual(pulse.pending_orders["partial-close"]["filled_qty"], .05)

    def test_confirmed_late_core_delta_after_flat_reopens_a_new_lot_epoch(self):
        pulse, position, parent, _, _ = self.partitioned_fixture()
        cid, oid = position.client_id, position.order_id
        row = {"client_id": cid, "order_id": oid, "symbol": position.symbol, "side": position.side,
               "requested_qty": .1, "filled_qty": .05, "avg_price": 100,
               "metadata": {"set_id": parent.id, "set_idx": parent.idx, "pack": "general",
                            "ind_config": "general_signal", "sl_pct": position.sl_pct, "tp_pct": position.tp_pct}}
        pulse.pending_orders[cid] = dict(row)
        self.assertTrue(pulse._record_close_fill(position, .1, 101, "tp", close_cid="flat-close", cumulative_qty=.1))
        row["filled_qty"] = .1
        reopened = pulse._upsert_pending_entry(row, .05, 110, oid, pending_qty=0)
        self.assertIsNotNone(reopened)
        self.assertAlmostEqual(pulse._dca_lot_ledger.parent_qty(cid), .05)
        pulse._restore_independent_close_state()
        self.assertEqual(len(pulse.open), 1)
        self.assertTrue(pulse._record_close_fill(reopened, .05, 111, "tp", close_cid="reopened-close", cumulative_qty=.05))
        self.assertNotEqual(pulse.closed[0].client_id, pulse.closed[-1].client_id)
        self.assertEqual(pulse.closed[-1].set_id, parent.id)
        self.assertAlmostEqual(pulse.closed[-1].entry, 110)

    def test_restart_retains_exact_close_config_and_measured_zero_cost(self):
        pulse, position, parent, child, _ = self.partitioned_fixture(free_entry=True, child_fee_known=True)
        self.assertTrue(pulse._record_close_fill(position, .1, 101, "tp", exit_fee_known=True))
        pulse.closed.clear()
        pulse._load_trade_history()
        self.assertEqual({row.set_id for row in pulse.closed}, {parent.id, child.id})
        self.assertTrue(all(row.ind_config == "general_signal" for row in pulse.closed))
        self.assertTrue(all(row.position_cost_pct == 0 for row in pulse.closed))
        self.assertTrue(all(pulse.cid_ours(row.client_id) for row in pulse.closed))

    def test_missing_close_parent_preserves_all_applied_cumulative_markers(self):
        pulse, position, _, _, _ = self.partitioned_fixture(free_entry=True, child_fee_known=True)
        cid = pulse.cid("c", position)
        self.assertTrue(pulse._record_close_fill(position, .02, 101, "tp", close_cid=cid,
                                               cumulative_qty=.02, exit_fee=.002, exit_fee_known=True))
        pulse.pending_orders[cid] = {"kind": "close", "client_id": cid, "symbol": position.symbol,
                                     "side": position.side, "requested_qty": .1, "filled_qty": .02,
                                     "fee_total": .002, "avg_price": 101, "group_key": position.control_group_key,
                                     "metadata": {"reason": "tp", "parent_client_id": position.client_id}}
        order = {"symbol": position.symbol, "positionSide": "LONG", "orderId": "partial-close-order",
                 "executedQty": .04, "avgPrice": 102, "commission": .004, "status": "PARTIALLY_FILLED"}
        track = pulse.parse_track(cid)
        pulse.open.clear()
        self.assertFalse(pulse._sync_pending_fill(order, cid, track, "c"))
        deferred = pulse.pending_orders[cid]
        self.assertAlmostEqual(deferred["filled_qty"], .02)
        self.assertAlmostEqual(deferred["avg_price"], 101)
        self.assertAlmostEqual(deferred["fee_total"], .002)
        pulse.open[pulse.position_key(position)] = position
        self.assertTrue(pulse._sync_pending_fill(order, cid, track, "c"))
        self.assertAlmostEqual(position.qty, .06)
        self.assertTrue(all(row.exit == 103 for row in pulse.closed[-2:]))
        self.assertAlmostEqual(sum(row.exit_fee for row in pulse.closed[-2:]), .002)
        self.assertAlmostEqual(sum(row.pnl for row in pulse.closed), .276)

    def test_unselected_preflight_cannot_borrow_a_qualified_pooled_set(self):
        pulse = Pulse.__new__(Pulse)
        pulse.px, pulse.open = {"X": 100}, {}
        pulse.occupying = lambda *args, **kwargs: False
        pooled = SimpleNamespace(id="pooled-winner")
        pulse.sets = SimpleNamespace(enabled=True, use_historic_gate=False, pick_entry=lambda *args, **kwargs: pooled,
                                     execution_allowed=lambda *args: True, pack_entry_sets=lambda *args: [])
        self.assertEqual(pulse.entry_sense("X", 1, "gen:trend", .8, "general"), "set-gate")
        ind = SimpleNamespace(kind="signals", mode="direct_tf", timeframe="5m", direction="long")
        pulse.strat_ind = True
        pulse.indications = SimpleNamespace(settings={"enabled": True}, match=lambda *args: ind)
        pulse.sets.indication_entry_sets = lambda *args: []
        self.assertEqual(pulse.entry_sense("X", 1, "ind:signals:direct_tf", .8, "indications"), "set-gate")

    def test_first_dca_after_core_partial_preserves_the_full_original_roundtrip(self):
        pulse, position, parent, child, _ = self.partitioned_fixture(preclose=True)
        self.assertAlmostEqual(position.qty, .075)
        self.assertTrue(pulse._record_close_fill(position, .075, 102, "tp", close_cid="after-dca-close", cumulative_qty=.075))
        own_base = parent.evaluation_live()
        own_child = child.evaluation_live()
        self.assertEqual((len(own_base), len(own_child)), (1, 1))
        self.assertAlmostEqual(own_base[0]["qty"], .05)
        self.assertAlmostEqual(own_base[0]["entry"], 100)
        self.assertAlmostEqual(own_base[0]["pnl"], .075 - .0075)
        self.assertAlmostEqual(own_child[0]["qty"], .05)
        self.assertAlmostEqual(own_child[0]["entry"], 90)
        self.assertAlmostEqual(sum(row.qty for row in pulse.closed), .1)

    def test_equal_add_deltas_separated_by_a_close_have_distinct_cumulative_ids(self):
        pulse, position, _, _, _ = self.partitioned_fixture()
        child_cid = next(cid for cid, row in pulse.pending_orders.items() if (row.get("metadata") or {}).get("dca_child_set_id"))
        self.assertTrue(pulse._record_close_fill(position, .05, 101, "tp", close_cid="intervening-close", cumulative_qty=.05))
        self.assertAlmostEqual(position.qty, .05)
        pulse._apply_position_fill(position, .05, 90, source="dca", fill_cid=child_cid,
                                   order_id="dca-order", fill_sequence=.1)
        self.assertAlmostEqual(position.qty, .1)
        self.assertAlmostEqual(pulse._dca_lot_ledger.parent_qty(position.client_id), .1)

    def test_fee_and_price_only_order_update_refines_own_open_child_without_new_qty(self):
        pulse, position, _, child, _ = self.partitioned_fixture()
        cid = next(cid for cid, row in pulse.pending_orders.items() if (row.get("metadata") or {}).get("dca_child_set_id"))
        self.assertTrue(pulse._refine_confirmed_order({"executedQty": .05, "avgPrice": 91,
                                                     "commission": .002, "orderId": "dca-order"}, cid, "d"))
        self.assertAlmostEqual(position.qty, .1)
        self.assertAlmostEqual(position.entry, 95.5)
        self.assertAlmostEqual(position.entry_fee, .002)
        own = pulse._dca_lot_ledger.entry_lots(cid)[0]
        self.assertEqual(own["set_id"], child.id)
        self.assertAlmostEqual(own["entry"], 91)
        self.assertAlmostEqual(own["entry_fee"], .002)
        self.assertTrue(own["entry_fee_known"])

    def test_fee_only_update_after_partial_close_preserves_applied_markers_for_cash_reconciliation(self):
        pulse, position, _, _, _ = self.partitioned_fixture()
        cid = next(cid for cid, row in pulse.pending_orders.items() if (row.get("metadata") or {}).get("dca_child_set_id"))
        self.assertTrue(pulse._record_close_fill(position, .05, 101, "tp", close_cid="partial", cumulative_qty=.05))
        self.assertFalse(pulse._refine_confirmed_order({"executedQty": .05, "avgPrice": 90,
                                                      "commission": .002, "orderId": "dca-order"}, cid, "d"))
        self.assertAlmostEqual(position.entry_fee, 0)
        self.assertAlmostEqual(pulse.pending_orders[cid]["fee_total"], 0)
        deferred = pulse.pending_orders[cid]["metadata"]["deferredFeePriceReconciliation"]
        self.assertAlmostEqual(deferred["feeTotal"], .002)

    def test_restart_receipt_restores_close_price_and_fees_even_without_an_open_parent(self):
        pulse, position, _, _, _ = self.partitioned_fixture(free_entry=True, child_fee_known=True)
        cid = pulse.cid("c", position)
        self.assertTrue(pulse._record_close_fill(position, .02, 101, "tp", close_cid=cid,
                                               cumulative_qty=.02, exit_fee=.002, exit_fee_known=True))
        pulse.open.clear()
        pulse.pending_orders[cid] = {"kind": "close", "client_id": cid, "symbol": position.symbol,
                                     "side": position.side, "requested_qty": .1, "filled_qty": 0,
                                     "fee_total": 0, "avg_price": 0, "group_key": position.control_group_key,
                                     "metadata": {"parent_client_id": position.client_id, "reason": "tp"}}
        pulse._restore_independent_close_state()
        self.assertAlmostEqual(pulse.pending_orders[cid]["filled_qty"], .02)
        self.assertAlmostEqual(pulse.pending_orders[cid]["avg_price"], 101)
        self.assertAlmostEqual(pulse.pending_orders[cid]["fee_total"], .002)

    def test_price_only_refinement_retains_previously_measured_child_fee(self):
        pulse, position, _, _, _ = self.partitioned_fixture(child_fee=.002, child_fee_known=True)
        cid = next(cid for cid, row in pulse.pending_orders.items() if (row.get("metadata") or {}).get("dca_child_set_id"))
        self.assertTrue(pulse._refine_confirmed_order({"executedQty": .05, "avgPrice": 91,
                                                     "orderId": "dca-order"}, cid, "d"))
        lot = pulse._dca_lot_ledger.entry_lots(cid)[0]
        self.assertTrue(lot["entry_fee_known"])
        self.assertAlmostEqual(lot["entry_fee"], .002)

    def test_additional_count_gate_receives_own_measured_rows_instead_of_cash_pnl(self):
        pulse = Pulse.__new__(Pulse)
        row = SimpleNamespace(pnl=10., pnl_pct=.002, qty=100., entry=100., position_cost_pct=.1,
                              axis_key="block-active:2", set_id="own", side="LONG")
        gate = Mock(return_value=(True, [], {"lastPf": 1.1}))
        pulse.coord = SimpleNamespace(last={}, add_gate=gate, add_stack_cap=lambda stack, pf: stack)
        pulse.block = SimpleNamespace(max_stack=6, count_tape={2: [100000.]})
        pulse.config_strategy_closes = lambda *args: [row]
        view = {"real_pf": 1.2, "real_n": 30}
        pulse.sets = SimpleNamespace(sets={"own": object()}, _side_view=lambda *args: view,
                                     _real_metrics_ok=lambda metrics: True)
        self.assertTrue(pulse._coord_add_state(count=2, set_id="own", side="LONG", strategy="block")[0])
        self.assertEqual(gate.call_args.kwargs["count_tape"], [row])
        gate.return_value = (False, ["count PF"], {"lastPf": 0., "last15Ratio": 1.7})
        result = pulse._coord_add_state(count=2, set_id="own", side="LONG", strategy="block")
        self.assertFalse(result[0])
        self.assertEqual(result[2], 0.)

    def test_symbol_summary_preserves_zero_return_pf_and_unavailable_evidence(self):
        pulse = Pulse.__new__(Pulse)
        pulse.position_cost_pct = .15
        losing = {"t": 1, "symbol": "X", "side": "LONG", "qty": 1, "entry": 100,
                  "pnl_pct": -.0135, "pnl": -1.5}
        self.assertEqual(pulse._by_symbol_blob([losing])[0]["pf"], 0.)
        self.assertIsNone(pulse._by_symbol_blob([{"t": 1, "symbol": "X", "side": "LONG"}])[0]["pf"])

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
