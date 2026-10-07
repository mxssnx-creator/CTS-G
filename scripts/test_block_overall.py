"""Block Overall: one shared lane per symbol x side, additive counts, close lifecycle.

Offline: a fake exchange records every order; nothing reaches a venue.
"""
import os
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
import pulse_trader as pt  # noqa: E402
from block_engine import BlockBook  # noqa: E402
from pulse_trader import Contract  # noqa: E402
from set_engine import SetBook, SetState  # noqa: E402

SYM = "TST-USDT"


class FakeApi:
    def __init__(self):
        self.posts = []
        self.path_cd = {}

    def post(self, path, body):
        self.posts.append((path, dict(body)))
        return {"code": 0, "data": {"order": {
            "orderId": f"oid{len(self.posts)}", "avgPrice": "100.5",
            "quantity": str(body.get("quantity"))}}}

    def get(self, path, params=None):
        return {"code": 0, "data": []}


def position(set_id, qty=0.05, side="LONG", micro=False, group=""):
    pos = pt.Position(symbol=SYM, side=side, qty=qty, entry=100.0, opened_at=time.time() - 600,
                      sl=99.0, tp=103.0, peak=100.0, set_id=set_id, pack="general")
    pos.micro = micro
    pos.control_group_key = group
    return pos


def trader(tmp, positions, block_active=True, stack=6):
    p = object.__new__(pt.Pulse)
    p.api = FakeApi()
    p.halted = False
    p.block = BlockBook(os.path.join(tmp, f"block-{time.time_ns()}.json"), {
        "variantBlockEnabled": True, "blockMaxStack": stack, "blockVolumeRatio": 0.25,
        "blockMaxVolumeMultiplier": 2.0, "blockProfitFactorRatio": 1.1, "defaultMinPF": 1.1,
        "blockActiveRealEnabled": True, "blockActiveLiveEnabled": True})
    p.dca = BlockDcaStub()
    p.strat_block = True
    p.block_active = block_active
    p.block_overall = True
    p.dca_overall = True
    p.available = 100.0
    p.block_last_emit = 0.0
    p.cooldown = {}
    p.errors = 0
    p.last_error = ""
    p.pending_orders = {}
    p._save_pending_orders = lambda: None
    p.seen_fill_cids = set()
    p.skip_log = {}
    p.did_io = False
    p.entries_blocked = lambda: False
    p.missing_controls = lambda pos: False
    p.ensure_controls = lambda pos: None
    p.control_orders = False
    p.save_open_book = lambda: None
    p.cid = lambda kind="o", pos=None, **kw: f"GTEST{len(p.api.posts)}"
    p.ok = lambda r: r.get("code") == 0
    p.sets = SetBook()
    p.sets.min_samples = 8
    p.sets.sets = {
        sid: SetState(id=sid, pack="general", tf="1m", sl_ratio=.6, trail_key="", trail_arm=0,
                      trail_give=0, last15_ratio=1.5, last15_n=12)
        for sid in {pos.set_id for pos in positions}
    }
    p.score = lambda sym: (1, "t", 0.9)
    p.indications = NS(best=lambda s: None, primary=lambda s: None)
    p.contracts = {SYM: Contract(SYM, 0.0001, 0.0001, 4, 2, 1.0, 100)}
    p.px = {SYM: 101.0}
    p.last_px = {}
    p.sl_min, p.sl_max, p.tp_min, p.tp_max = 0.001, 0.02, 0.002, 0.05
    p.position_cost_pct = 0.10
    p.tp_cost_ratio = 1.5
    p.exits = NS(enabled=False, opt_sl_min=0.001, opt_sl_max=0.009)
    p.lev_map = {SYM: 100}
    p.lev_max = {SYM: 100}
    p.notional_cap = lambda: 10**9
    p.max_book_notional = lambda *a, **k: 10**9
    p.cap_order_qty = lambda c, px, qty, cap=None: float(qty)
    p.min_order_qty = lambda c, px: float(c.min_qty)
    p.leverage_for = lambda c: 100
    p.coord = NS(min_pf=1.10, real_eval=3, stage_min_pf={"real": 1.10}, last={},
                 add_gate=lambda *a, **k: (True, [], {"lastPf": 1.2}),
                 add_stack_cap=lambda stack, pf: stack)
    p.closed = [NS(symbol=SYM, side="LONG", pnl=0.004, pnl_pct=0.004, t=time.time() - i,
                   ours=True, member_count=1, exchange_confirmed=True, client_id=f"c{i}",
                   qty=1.0, entry=100.0) for i in range(12)]
    p.open = {f"{SYM}:{pos.side}:{pos.set_id}": pos for pos in positions}
    for pos in positions:
        if not pos.micro:
            p.ensure_strategy_lanes(pos)
    return p


class BlockDcaStub:
    """DCA stand-in that records drops; the DCA book itself is tested elsewhere."""

    enabled = False
    max_steps = 0

    def __init__(self):
        self.dropped = []
        self.closes = []
        self.lanes = {}

    def key(self, symbol, side, group_key=""):
        return f"{symbol}:{side}:group:{group_key}" if group_key else f"{symbol}:{side}"

    def attach(self, *a, **k):
        return None

    def on_close(self, rec):
        self.closes.append(dict(rec))

    def drop(self, symbol, side, group_key=""):
        self.dropped.append(self.key(symbol, side, group_key))


def run_adds(p, rounds):
    for _ in range(rounds):
        p.block_last_emit = 0.0
        p.maybe_block_adds()
        p.pending_orders = {}


class OverallLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_one_shared_lane_per_symbol_side_across_sets(self):
        a, b = position("general:sl0.6:st3", group="g1"), position("general:sl0.8:st5", group="g2")
        p = trader(self.tmp.name, [a, b])
        self.assertEqual(set(p.block.lanes), {f"{SYM}:LONG"})
        self.assertAlmostEqual(p.block.lanes[f"{SYM}:LONG"].base_qty, 0.10, places=9)
        run_adds(p, 1)
        self.assertEqual(len(p.api.posts), 1, "one add for the shared lane, not one per Set")
        self.assertAlmostEqual(float(p.api.posts[0][1]["quantity"]), 0.025, places=6)

    def test_additive_counts_stack_to_the_volume_cap(self):
        p = trader(self.tmp.name, [position("general:sl0.6:st3")])
        p.px[SYM] = 102.0  # 2% continuation clears every count's n x 0.2%
        run_adds(p, 8)
        lane = p.block.lanes[f"{SYM}:LONG"]
        counts = [leg.block_count for leg in lane.legs]
        self.assertEqual(counts, [1, 2, 3, 4], "1x parent cap = four 0.25 adds")
        self.assertAlmostEqual(lane.confirmed_add, 0.05, places=6)
        self.assertLessEqual(lane.confirmed_add, lane.base_qty * p.block.extra_cap() + 1e-9)

    def test_stack_cap_bounds_the_counts(self):
        p = trader(self.tmp.name, [position("general:sl0.6:st3")], stack=2)
        p.px[SYM] = 102.0
        run_adds(p, 6)
        self.assertEqual([leg.block_count for leg in p.block.lanes[f"{SYM}:LONG"].legs], [1, 2])

    def test_each_count_needs_its_own_continuation(self):
        p = trader(self.tmp.name, [position("general:sl0.6:st3")])
        p.px[SYM] = 100.7  # counts 1 and 2 clear 0.2%/0.4% over the averaged entry, count 3 not 0.6%
        run_adds(p, 6)
        self.assertEqual([leg.block_count for leg in p.block.lanes[f"{SYM}:LONG"].legs], [1, 2])

    def test_failed_count_gate_stops_only_that_count(self):
        p = trader(self.tmp.name, [position("general:sl0.6:st3")])
        p.px[SYM] = 102.0
        p._coord_add_state = lambda **k: ((k["count"] != 2), 0, 0.0, ["count 2 blocked"])
        run_adds(p, 4)
        self.assertEqual([leg.block_count for leg in p.block.lanes[f"{SYM}:LONG"].legs], [1])

    def test_min_lot_parent_keeps_room_for_its_add_at_the_shipped_volume_factor(self):
        # Venue minimum lot 8 USDT, sized notional 2.15 x 0.1: the parent opens
        # at the minimum lot and its 1x add must not be starved by the 2 USDT cap.
        p = trader(self.tmp.name, [position("general:sl0.6:st3", qty=0.08)])
        p.contracts[SYM] = Contract(SYM, 0.0001, 0.0001, 4, 2, 8.0, 100)
        p.min_order_qty = lambda c, px: max(float(c.min_qty), float(c.min_usdt) / px)
        p.block.lanes.clear()
        p.ensure_strategy_lanes(next(iter(p.open.values())))
        p.volume_factor = 0.1
        p.vol1h = {}
        p.coord.size_mult = lambda n: 1.0
        p.margin_cap_pct = 0.0
        p.equity = 100.0
        for name in ("max_book_notional", "notional_cap", "cap_order_qty"):
            if name in vars(p):
                delattr(p, name)
        p.px[SYM] = 102.0
        run_adds(p, 1)
        self.assertEqual(len(p.api.posts), 1, "min-lot parent gets its add")
        self.assertGreaterEqual(float(p.api.posts[0][1]["quantity"]) * 102.0, 7.9)

    def test_micro_parent_gets_no_add_and_is_not_parent_size(self):
        p = trader(self.tmp.name, [position("general:sl0.6:st3", micro=True)])
        p.block.register_parent(SYM, "LONG", 0.05, 100.0)
        run_adds(p, 2)
        self.assertEqual(p.api.posts, [])
        core = position("general:sl0.8:st5", qty=0.04)
        p.open["core"] = core
        self.assertAlmostEqual(p._block_core_qty(SYM, "LONG"), 0.04, places=9)

    def test_pending_add_owns_the_shared_lane_for_every_parent(self):
        a, b = position("general:sl0.6:st3", group="g1"), position("general:sl0.8:st5", group="g2")
        p = trader(self.tmp.name, [a, b])
        p.per_config_controls = lambda pos: True
        p.pending_orders = {"x": {"kind": "block", "symbol": SYM, "side": "LONG", "group_key": "g1",
                                  "requested_qty": 0.025, "filled_qty": 0}}
        self.assertTrue(p._pending_add_open(b, "block"))
        p.block_overall = False
        self.assertFalse(p._pending_add_open(b, "block"))


class CloseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def closed(self, pos, pnl_pct):
        return pt.Closed(t=time.time(), symbol=pos.symbol, side=pos.side, qty=pos.qty, entry=pos.entry,
                         exit=pos.entry, pnl=pnl_pct, pnl_pct=pnl_pct, reason="tp", hold_s=60.0,
                         exchange_confirmed=True, client_id=pos.client_id)

    def test_close_of_the_carrier_records_on_the_overall_lane_under_per_config_controls(self):
        a, b = position("general:sl0.6:st3", group="g1"), position("general:sl0.8:st5", group="g2")
        p = trader(self.tmp.name, [a, b])
        p.per_config_controls = lambda pos: True
        p.position_key = lambda pos: pos.control_group_key
        run_adds(p, 1)
        lane = p.block.lanes[f"{SYM}:LONG"]
        carrier = a if "block" in a.strategy else b
        other = b if carrier is a else a
        self.assertIn("block", carrier.strategy)
        self.assertGreater(lane.confirmed_add, 0)
        p._close_strategy_lanes(carrier, self.closed(carrier, 0.006), 0.006, 0.006)
        p.open = {k: v for k, v in p.open.items() if v is not carrier}
        self.assertEqual(len(lane.parent_pf_ring), 1, "sample recorded on the shared lane")
        self.assertEqual(lane.confirmed_add, 0.0)
        self.assertEqual(lane.legs, [])
        self.assertTrue(lane.active, "the other parent keeps the lane bound")
        self.assertIn(1, p.block.count_tape)
        # The remaining parent closes: sample recorded, lane released.
        p._close_strategy_lanes(other, self.closed(other, -0.002), -0.002, -0.002)
        self.assertEqual(len(lane.parent_pf_ring), 2)
        self.assertFalse(lane.active)
        self.assertEqual(lane.base_qty, 0.0)

    def test_non_carrier_close_leaves_the_add_on_the_lane(self):
        a, b = position("general:sl0.6:st3", group="g1"), position("general:sl0.8:st5", group="g2")
        p = trader(self.tmp.name, [a, b])
        p.per_config_controls = lambda pos: True
        p.position_key = lambda pos: pos.control_group_key
        run_adds(p, 1)
        lane = p.block.lanes[f"{SYM}:LONG"]
        other = b if "block" in a.strategy else a
        add = lane.confirmed_add
        p._close_strategy_lanes(other, self.closed(other, 0.003), 0.003, 0.003)
        self.assertEqual(lane.confirmed_add, add)
        self.assertEqual(len(lane.legs), 1)
        self.assertEqual(p.block.count_tape.get(1, []), [], "counts are credited by the carrier only")
        self.assertEqual(len(lane.parent_pf_ring), 1)

    def test_dca_overall_close_uses_the_shared_key(self):
        a = position("general:sl0.6:st3", group="g1")
        p = trader(self.tmp.name, [a])
        p.per_config_controls = lambda pos: True
        p.position_key = lambda pos: pos.control_group_key
        p._close_strategy_lanes(a, self.closed(a, 0.003), 0.003, 0.003)
        self.assertEqual(p.dca.dropped, [f"{SYM}:LONG"])
        self.assertEqual(p.dca.closes[-1]["control_group_key"], "")

    def test_per_lane_mode_keeps_group_keys(self):
        a = position("general:sl0.6:st3", group="g1")
        p = trader(self.tmp.name, [a])
        p.block_overall = False
        p.dca_overall = False
        p.per_config_controls = lambda pos: True
        p.position_key = lambda pos: pos.control_group_key
        p.block.register_parent(SYM, "LONG", 0.05, 100.0, group_key="g1")
        p._close_strategy_lanes(a, self.closed(a, 0.003), 0.003, 0.003)
        self.assertEqual(len(p.block.lanes[f"{SYM}:LONG:group:g1"].parent_pf_ring), 1)
        self.assertEqual(p.dca.dropped, [f"{SYM}:LONG:group:g1"])


class HistoricBlockEvidenceTests(unittest.TestCase):
    def test_block_rows_keep_the_lot_move_and_carry_the_volume_ratio(self):
        book = SetBook()
        pos = {"side": 1, "entry": 100.0, "sl": 99.0, "tp": 100.4, "i": 0, "qty": 0.25, "parent": 1.0,
               "peak": 100.0, "adds": 1}
        bar = (0, 100.5, 99.9, 100.45)  # (t, high, low, close)
        pos["tags"] = "block:active"
        _, rec = book._advance_pos(pos, bar, 1, 0.006, 0.004, False, 0.0, 0.0, 60, 60, True,
                                   time.time(), "SYM", "block-set", "general", "block")
        self.assertIsNotNone(rec)
        self.assertAlmostEqual(float(rec["pnl_pct"]), 0.004, places=6, msg="lot move, not 0.25 x move")
        self.assertAlmostEqual(float(rec["volume_ratio"]), 0.25, places=6)


if __name__ == "__main__":
    unittest.main()
