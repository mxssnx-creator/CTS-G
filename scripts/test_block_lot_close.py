"""Pure count outcomes conserve native lifecycle and measured Return PF."""
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/"server/pulse"))
from block_engine import BlockBook, cost_pf_from_net_fracs
from position_cost import last_n_cost_pf


def close(**changes):
    row = dict(conn="X02", t=1000., symbol="X", side="LONG", strategy="block",
        axis_key="block-add:2", set_id="parent:adjust:block-add:2", parent_set_id="parent",
        client_id="child", close_fill_id="close-1", exchange_confirmed=True,
        partial=False, ours=True, qty=1., entry=100., exit=101., pnl_pct=.01,
        position_cost_pct=.2, roundtrip_qty=1.)
    row.update(changes)
    return row


class BlockLotCloseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(pathlib.Path(self.tmp.name)/"block.json")
        self.cfg=dict(blockEvalPosCount=5,prevPosWindow=25,positionCostPct=.1)
        self.book=BlockBook(self.path,self.cfg)
        self.lane=self.book.register_parent("X","LONG",10.,100.,group_key="own")

    def tearDown(self):
        self.tmp.cleanup()

    def test_measured_cost_normalization_matches_canonical_return_pf(self):
        rows=[close(close_fill_id=f"close-{i}",client_id=f"child-{i}",
                    pnl_pct=.01+i*.0002,position_cost_pct=cost,t=1000+i)
              for i,cost in enumerate([.01,.1,.3,0.,.2])]
        for row in rows:
            self.assertTrue(self.book.record_lot_close(row,group_key="own"))
        expected=last_n_cost_pf(rows,len(rows),self.book.cost_pct)["ratio"]
        actual=cost_pf_from_net_fracs(self.lane.pf_ring[2],self.book.cost_pct)
        self.assertAlmostEqual(actual,expected,places=4)
        self.assertEqual(self.lane.pf_ring[1] if 1 in self.lane.pf_ring else [],[])
        self.assertEqual(len(self.book.count_tape[2]),5)
        self.assertEqual(self.book.overall_tape,[])

    def test_only_the_own_count_pause_and_recovery_change(self):
        self.lane.pause_remaining[1]=4
        self.lane.held_factor[1]=5.
        self.lane.held_factor[2]=4.
        self.lane.confirmed_add=2.5
        self.lane.satisfied[1]=True
        self.assertTrue(self.book.record_lot_close(close(),group_key="own"))
        self.assertEqual(self.lane.pause_remaining[1],4)
        self.assertEqual(self.lane.held_factor[1],5.)
        self.assertEqual(self.lane.pause_remaining[2],self.book.pause_ratio)
        self.assertEqual(self.lane.held_factor[2],1.)
        self.assertTrue(self.lane.active)
        self.assertEqual(self.lane.base_qty,10.)
        self.assertEqual(self.lane.confirmed_add,2.5)
        self.assertTrue(self.lane.satisfied[1])
        bad=close(client_id="loser",close_fill_id="loss",pnl_pct=-.001)
        self.assertTrue(self.book.record_lot_close(bad,group_key="own"))
        self.assertEqual(self.lane.held_factor[2],2.)

    def test_lifecycle_only_close_does_not_consume_new_own_count_pause(self):
        self.book.record_lot_close(close(),group_key="own")
        before=self.lane.pause_remaining[2]
        self.book.on_parent_close("X","LONG",0.,group_key="own",record=False,keep_lane=True)
        self.assertEqual(self.lane.pause_remaining[2],before)
        self.assertEqual(len(self.lane.pf_ring[2]),1)
        self.assertEqual(self.lane.parent_pf_ring,[])

    def test_duplicate_completed_close_stays_idempotent_after_restart(self):
        row=close()
        self.assertTrue(self.book.record_lot_close(row,group_key="own"))
        self.assertFalse(self.book.record_lot_close(row,group_key="own"))
        restored=BlockBook(self.path,self.cfg)
        self.assertFalse(restored.record_lot_close(row,group_key="own"))
        self.assertEqual(len(restored.lanes[restored.key("X","LONG","own")].pf_ring[2]),1)
        self.assertEqual(len(restored.count_tape[2]),1)

    def test_incomplete_or_aggregate_evidence_is_not_a_count_outcome(self):
        for patch in [dict(partial=True),dict(exchange_confirmed=False),dict(ours=False),
                      dict(strategy="core+block"),dict(qty=.5,roundtrip_qty=1.),
                      dict(close_fill_id=""),dict(pnl_pct=float("nan")),
                      dict(axis_key="",block_count=0),dict(block_count=7)]:
            with self.subTest(patch=patch):
                self.assertFalse(self.book.record_lot_close(close(**patch),group_key="own"))
        self.assertEqual(self.lane.pf_ring,{})
        self.assertTrue(all(not tape for tape in self.book.count_tape.values()))

    def test_scope_and_direction_do_not_borrow_a_sibling_outcome(self):
        sibling=self.book.register_parent("X","LONG",10.,100.,group_key="sibling")
        opposite=self.book.register_parent("X","SHORT",10.,100.,group_key="own")
        row=close(control_group_key="own")
        self.assertTrue(self.book.record_lot_close(row))
        self.assertTrue(self.lane.pf_ring[2])
        self.assertEqual(sibling.pf_ring,{})
        self.assertEqual(opposite.pf_ring,{})

    def test_pure_parent_and_child_tapes_remain_separate(self):
        row=close(strategy="core",axis_key="",set_id="parent",client_id="base",close_fill_id="base-close")
        self.assertTrue(self.book.record_parent_lot_close(row,group_key="own"))
        self.assertFalse(self.book.record_parent_lot_close(close(),group_key="own"))
        self.assertEqual(self.lane.pf_ring,{})
        self.assertEqual(len(self.lane.parent_pf_ring),1)
        self.assertEqual(len(self.book.overall_tape),1)
        self.assertAlmostEqual(self.book.normal_pf(self.lane),
                               last_n_cost_pf([row],1,self.book.cost_pct)["ratio"],places=4)

    def test_later_own_parent_releases_pause_without_consuming_same_close(self):
        self.book.record_lot_close(close(t=1000.),group_key="own")
        base=close(strategy="core",axis_key="",set_id="parent",client_id="base",close_fill_id="base-1",t=1000.)
        self.assertTrue(self.book.record_parent_lot_close(base,group_key="own"))
        self.assertEqual(self.lane.pause_remaining[2],self.book.pause_ratio)
        later=dict(base,client_id="later",close_fill_id="base-2",t=1100.)
        self.assertTrue(self.book.record_parent_lot_close(later,group_key="own"))
        self.assertEqual(self.lane.pause_remaining[2],max(0,self.book.pause_ratio-1))
        self.assertFalse(self.book.record_parent_lot_close(later,group_key="own"))

    def test_parent_pause_progress_stays_scoped_and_survives_restart(self):
        self.book.record_lot_close(close(),group_key="own")
        restored=BlockBook(self.path,self.cfg)
        lane=restored.lanes[restored.key("X","LONG","own")]
        self.assertEqual(lane.pause_started_t[2],1000.)
        base=close(strategy="core",axis_key="",set_id="parent",client_id="base",close_fill_id="base",t=1100.)
        restored.record_parent_lot_close(base,group_key="sibling")
        self.assertEqual(lane.pause_remaining[2],self.book.pause_ratio)
        restored.record_parent_lot_close(dict(base,close_fill_id="own-base"),group_key="own")
        self.assertEqual(lane.pause_remaining[2],max(0,self.book.pause_ratio-1))

    def test_cost_scale_edit_preserves_each_completed_lots_return_pf(self):
        self.book.record_lot_close(close(),group_key="own")
        base=close(strategy="core",axis_key="",set_id="parent",client_id="base",close_fill_id="base")
        self.book.record_parent_lot_close(base,group_key="own")
        before=cost_pf_from_net_fracs(self.lane.pf_ring[2],self.book.cost_pct)
        parent_before=self.book.normal_pf(self.lane)
        self.assertTrue(self.book.set_cost_pct(.3))
        self.assertAlmostEqual(cost_pf_from_net_fracs(self.lane.pf_ring[2],self.book.cost_pct),before)
        self.assertAlmostEqual(self.book.normal_pf(self.lane),parent_before)
        self.assertTrue(self.book.set_cost_pct(0.))
        self.assertEqual(self.book.cost_pct,0.)
        self.assertAlmostEqual(cost_pf_from_net_fracs(self.book.count_tape[2],self.book.cost_pct),before)
        self.assertFalse(self.book.set_cost_pct(0.))

    def test_restart_rescales_saved_return_units_to_new_configured_cost(self):
        self.book.record_lot_close(close(),group_key="own")
        before=cost_pf_from_net_fracs(self.lane.pf_ring[2],self.book.cost_pct)
        restored=BlockBook(self.path,dict(self.cfg,positionCostPct=.4))
        lane=restored.lanes[restored.key("X","LONG","own")]
        self.assertAlmostEqual(cost_pf_from_net_fracs(lane.pf_ring[2],restored.cost_pct),before)
        self.assertAlmostEqual(cost_pf_from_net_fracs(restored.count_tape[2],restored.cost_pct),before)


if __name__ == "__main__":
    unittest.main()
