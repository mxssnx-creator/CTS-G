"""Measured/fallback costs, propagation and cumulative fill accounting."""
import pathlib
import sys
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server'/'pulse'))
import pulse_trader as pt
from position_cost import effective_position_cost_pct
from set_engine import SetBook
from coord_engine import Coordinator
from hist_calc import overlay_from_options, parse_options

class DynamicCostPolicyTests(unittest.TestCase):
    def test_defaults_and_explicit_fallback_precedence(self):
        p=pt.Pulse.__new__(pt.Pulse)
        self.assertEqual(p._position_cost_config({},{}),(.1,True))
        self.assertEqual(p._position_cost_config({'positionCostPct':.087,'positionCostFallbackPct':.1},{}),(.1,True))
        self.assertEqual(effective_position_cost_pct([])['costPct'],.1)
        b=SetBook();b.load({});c=Coordinator();c.load({},{})
        self.assertEqual(b.max_dd_s,57600)
        self.assertEqual(b.stage_min_pf,dict(base=1.10,main=1.10,real=1.10))
        self.assertEqual(c.min_pf,1.10)
        h=overlay_from_options(parse_options({}))
        self.assertEqual((h['setMinPf'],h['setMaxDdTimeS'],h['positionCostPct']),(1.10,57600,.1))

    def test_measured_cost_propagates_to_all_calculators(self):
        p=pt.Pulse.__new__(pt.Pulse);p.manual_position_cost_pct=.1
        p.coord=NS(cost_pct=.1);p.sets=NS(cost_pct=.1,by_idx=[])
        p.dca=NS(cost_pct=.1);p.exits=NS(cost_pct=.1);p.indications=NS(settings={})
        p._score_cache={'old':1}
        p._apply_effective_position_cost(.08,'live-exchange')
        for c in (p.coord,p.sets,p.dca,p.exits):self.assertEqual(c.cost_pct,.08)
        self.assertEqual(p.coord.position_cost_pct,.08)
        self.assertEqual(p.indications.settings['positionCostPct'],.08)
        self.assertEqual(p._score_cache,{})
        self.assertEqual(p.manual_position_cost_pct,.1)

    def test_owned_cumulative_fills_replace_samples_and_ignore_foreign(self):
        p=pt.Pulse.__new__(pt.Pulse);p.use_live_position_costs=True
        p.manual_position_cost_pct=.1;p._live_cost_lock=threading.RLock()
        p._live_cost_rows=[];p._live_cost_seen={}
        p.order_cid=lambda r:r['clientOrderID'];p.cid_ours=lambda c:c.startswith('owned-')
        p.parse_track=lambda c:dict(kind='o')
        p._save_live_cost_state=Mock();p._apply_effective_position_cost=Mock()
        row=dict(orderId='12345',clientOrderID='owned-1',executedQty=10,avgPrice=100,commission=.15)
        p._update_live_position_costs([row,dict(row,clientOrderID='foreign-1',orderId='99999',commission=100)])
        self.assertEqual(p.live_position_cost_samples,1)
        self.assertAlmostEqual(p.live_position_cost_pct,.03)
        p._update_live_position_costs([row]);self.assertEqual(p._save_live_cost_state.call_count,1)
        p._update_live_position_costs([dict(row,executedQty=20,commission=.4)])
        self.assertEqual(p.live_position_cost_samples,1)
        self.assertAlmostEqual(p.live_position_cost_pct,.04)
        self.assertTrue(p.live_position_cost_complete)
        self.assertEqual(p._save_live_cost_state.call_count,2)

if __name__=='__main__':unittest.main()
