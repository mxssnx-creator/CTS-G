import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fetch_historic_window import validate
from replay_five_days import configs, replay
import pulse_trader


class HistoricTests(unittest.TestCase):
    def run_lane(self, changes=None, cfg=None, cost=0, side=1):
        bars = [[100.,100.,100.,100.,1.] for _ in range(10)]
        for i, b in (changes or {}).items(): bars[i] = b
        return replay(bars, [(side,1)]+[(0,0)]*9, side,
                      cfg or [dict(strategy='base',levels=0,incrementPct=0,volumeRatio=0,tpPct=.5,slPct=.2)],
                      warmup=0,cost_pct=cost)

    def test_complete_grid(self):
        self.assertEqual(len(configs())*3*8*2,62208)
        self.assertEqual(len({tuple(x.items()) for x in configs()}),1296)

    def test_gaps_duplicates_and_nan_rejected(self):
        valid = [(0,[1,2,.5,1,2]),(60000,[1,2,.5,1,2])]
        validate(valid,0,120000)
        for rows in (valid[:1], valid+[valid[-1]], [(0,[1,float('nan'),.5,1,2]),valid[1]]):
            with self.assertRaises(ValueError): validate(rows,0,120000)

    def test_stop_first_and_worse_gap_both_sides(self):
        self.assertAlmostEqual(self.run_lane({1:[100,101,99,100,1]})[0]['netPct'],-.2)
        self.assertAlmostEqual(self.run_lane({1:[99,100,98,99,1]})[0]['netPct'],-1)
        self.assertAlmostEqual(self.run_lane({1:[101,102,100,101,1]},side=-1)[0]['netPct'],-1)

    def test_entry_bar_cannot_exit_or_add(self):
        r=self.run_lane({0:[100,110,90,100,1]})[0]
        self.assertEqual(r['n'],1); self.assertEqual(r['netPct'],0); self.assertEqual(r['boundaryCloses'],1)

    def test_unrealized_loss_closed_at_split_not_censored(self):
        r=self.run_lane({i:[99.9,100,99.9,99.9,1] for i in range(1,10)})[0]
        self.assertAlmostEqual(r['netPct'],-.1); self.assertEqual(r['trainN'],1)
        self.assertEqual(r['holdoutN'],0); self.assertEqual(r['boundaryCloses'],1)
        self.assertEqual(sum(r['dailyN']),r['n']); self.assertAlmostEqual(sum(r['dailyNetPct']),r['netPct'])

    def test_dca_original_portion_and_each_execution_cost(self):
        cfg=[dict(strategy='dca',levels=1,incrementPct=.05,volumeRatio=.25,tpPct=2,slPct=2)]
        r=self.run_lane({1:[100,100,99.8,99.8,1]},cfg,cost=.15)[0]
        self.assertEqual(r['maxVolume'],1.25); self.assertEqual(r['additions'],1)
        self.assertAlmostEqual(r['costPct'],.1874625,delta=1e-6)
        self.assertAlmostEqual(r['netPct'],.05-.1874625,delta=1e-6)

    def test_no_dca_repair_after_stop(self):
        cfg=[dict(strategy='dca',levels=3,incrementPct=.05,volumeRatio=.25,tpPct=2,slPct=2)]
        r=self.run_lane({1:[100,100,97,97,1]},cfg)[0]
        self.assertEqual(r['additions'],0); self.assertEqual(r['maxVolume'],1); self.assertEqual(r['netPct'],-2)

    def test_block_counts_independent_additive_and_cap_two(self):
        cfg=[dict(strategy='block',levels=n,incrementPct=.2,volumeRatio=.25,tpPct=2,slPct=2) for n in range(1,7)]
        changes={1:[100,100.3,100,100.3,1]}
        rows=self.run_lane(changes,cfg)
        self.assertEqual([r['maxVolume'] for r in rows],[1.25,1.5,1.75,2,2,2])
        for i,c in enumerate(cfg):
            single=self.run_lane(changes,[c])[0]
            self.assertEqual({k:v for k,v in single.items() if k!='config'},
                             {k:v for k,v in rows[i].items() if k!='config'})


class ProbeIsolationTests(unittest.TestCase):
    def test_live_state_survives_probe_and_exception(self):
        for raises in (False,True):
            live=pulse_trader.Pulse.__new__(pulse_trader.Pulse)
            live.px={'BTC-USDT':123}; live.open={'parent':SimpleNamespace(qty=1)}
            live.closed=[SimpleNamespace(pnl=2)]; live.available=7; reports=[]
            live.record_test=lambda *args:reports.append(args)
            def fake(probe):
                probe.px['BTC-USDT']=80000; probe.open['parent'].qty=99
                probe.closed[0].pnl=-9; probe.closed=[]; probe.available=80
                live.closed.append(SimpleNamespace(pnl=3)); live.px['ETH-USDT']=456
                live.available=6; probe.record_test('isolated',True,'')
                if raises: raise RuntimeError('probe failure')
            with patch.object(pulse_trader.Pulse,'_run_self_tests_isolated',fake):
                if raises:
                    with self.assertRaises(RuntimeError): live.run_self_tests()
                else: live.run_self_tests()
            self.assertEqual(live.px,{'BTC-USDT':123,'ETH-USDT':456})
            self.assertEqual(live.open['parent'].qty,1); self.assertEqual(live.available,6)
            self.assertEqual([r.pnl for r in live.closed],[2,3]); self.assertEqual(len(reports),1)


if __name__=='__main__': unittest.main()
