"""The baseline request uses one worker, scoped evidence and current controls."""
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server/pulse'))
import pulse_trader as pt
import hist_calc as hc
from system_settings import calculation_overlay


def candidate(**changes):
    return dict(id='forced:one',symbol='SOL-USDT',indication='signals',direction='LONG',tpPct=.4,slPct=.1,
        pf=.4,trainPf=2,holdoutPf=0,trainN=10,holdoutN=8,maxDrawdownR=10,trainingMaxDrawdownR=1,
        trainingWindowsOk=True,evidenceVersion=2,tradesPerHour=1,eligible=False,**changes)


class ForcedQueueTests(unittest.TestCase):
    def pulse(self):
        p=object.__new__(pt.Pulse)
        p.overlay=calculation_overlay({'controlMinTrades':0})
        p.coord=SimpleNamespace(min_pf=1.02)
        p._hist_stop=threading.Event()
        p._hist_request_changed=lambda:False
        return p

    def test_automatic_refresh_does_not_requeue_consumed_manual_work(self):
        p=self.pulse()
        # Use the real coalescing check, including its throttled branch.
        del p._hist_request_changed
        request={'runId':'manual:4'}
        p._hist_begin_request(request,'manual:4')
        p._hist_begin_request({},'automatic:5')
        with patch.object(pt,'read_hist_request',return_value=request):
            self.assertFalse(p._hist_request_changed())
            self.assertFalse(p._hist_request_changed())
            self.assertEqual(p._hist_new_request(consume=False),{})
        p._hist_request_check_ts=0
        with patch.object(pt,'read_hist_request',return_value={'runId':'manual:6'}):
            self.assertTrue(p._hist_request_changed())
            self.assertEqual(p._hist_new_request(consume=False)['runId'],'manual:6')
        self.assertEqual(p._hist_request_seen,'manual:4')

    def test_completed_baseline_stays_consumed_after_restart_in_its_own_connection(self):
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/'baseline.json'
            completed={'version':2,'connection':'bingx-x02','requestRunId':'manual:4'}
            path.write_text(json.dumps(completed))
            p=self.pulse();p._hist_request_seen=''
            with patch.object(pt,'CONN_SHORT','bingx-x02'),patch.object(pt,'forced_path',return_value=str(path)), \
                 patch.object(pt,'read_hist_request',return_value={'runId':'manual:4','forcedOnly':True}):
                self.assertEqual(p._hist_new_request(consume=False),{})
                p._hist_request_seen='';completed['connection']='bingx-x01';path.write_text(json.dumps(completed))
                self.assertEqual(p._hist_new_request(consume=False)['runId'],'manual:4')

    def test_queued_baseline_is_processed_with_saved_zero_and_does_not_change_main_book(self):
        p=self.pulse();book=p.sets=object()
        request=dict(runId='x02:7',generation=7,forcedOnly=True,hours=24)
        result=dict(phase='ready',ready=True,forcedConfigs={'version':2},_forcedMatrix=[{'symbol':'SOL-USDT'}])
        def calculate(body,**kw):
            self.assertEqual(body['overlay']['controlMinTrades'],0)
            self.assertFalse(kw['persist'])
            self.assertFalse(kw['should_cancel']())
            kw['on_progress']({'phase':'replay','pct':50})
            return result
        with patch.object(pt,'CONN_SHORT','bingx-x02'),patch.object(pt,'run_forced_calc',side_effect=calculate) as calc, \
             patch.object(pt,'atomic_write') as write,patch.object(pt,'write_hist_job') as publish,patch.object(pt.os.path,'exists',return_value=False):
            p._hist_run_forced(request)
        calc.assert_called_once()
        self.assertIs(p.sets,book)
        self.assertEqual(p._hist_request_seen,'x02:7')
        self.assertEqual(p._hist_active_run_id,'')
        self.assertIn('forced-configs-bingx-x02.json',str(write.call_args.args[0]))
        self.assertEqual(write.call_args.args[1]['connection'],'bingx-x02')
        self.assertNotIn('_forcedMatrix',publish.call_args.args[0])
        self.assertEqual(publish.call_args.args[1],'bingx-x02')

    def test_newer_queued_request_cannot_be_overwritten_or_publish_old_evidence(self):
        p=self.pulse();changed=[False];p._hist_request_changed=lambda:changed[0]
        def calculate(body,**kw):
            changed[0]=True
            self.assertTrue(kw['should_cancel']())
            return dict(phase='ready',ready=True,forcedConfigs={'version':2},_forcedMatrix=[])
        with patch.object(pt,'run_forced_calc',side_effect=calculate),patch.object(pt,'atomic_write') as write, \
             patch.object(pt,'write_hist_job') as publish:
            p._hist_run_forced(dict(runId='old',forcedOnly=True))
        write.assert_not_called()
        self.assertEqual(publish.call_count,1)  # initial status only

    def test_stop_remains_stopped_and_cannot_publish_candidates(self):
        p=self.pulse()
        def calculate(body,**kw):
            self.assertTrue(kw['should_cancel']())
            return {'phase':'error','detail':'cancelled'}
        with patch.object(pt,'read_hist_job',return_value={'phase':'stopped','runId':'stop-me'}), \
             patch.object(pt,'run_forced_calc',side_effect=calculate),patch.object(pt,'atomic_write') as write, \
             patch.object(pt,'write_hist_job') as publish:
            p._hist_run_forced(dict(runId='stop-me',forcedOnly=True))
        write.assert_not_called();publish.assert_not_called()

    def test_scoped_cache_reclassifies_each_set_when_zero_disables_control(self):
        with tempfile.TemporaryDirectory() as d:
            path=pathlib.Path(d)/'forced.json'
            blob=dict(version=2,connection='bingx-x02',baselineOnly=True,updatedAt=time.time(),
                sourceBySymbol={'SOL-USDT':'historical-market'},rows=[],matrix=[{'symbol':'SOL-USDT','rows':[candidate()]}])
            path.write_text(json.dumps(blob))
            p=self.pulse();p.overlay['controlMinTrades']=5
            with patch.object(pt,'CONN_SHORT','bingx-x02'),patch.object(pt,'forced_path',return_value=str(path)):
                self.assertEqual(p._forced_data()['rows'],[])
                p.overlay['controlMinTrades']=0
                rows=p._forced_data()['rows']
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['holdoutPf'],0)
                self.assertFalse(rows[0]['controlChecked'])
                self.assertIsNone(rows[0]['controlPassed'])
                self.assertEqual(rows[0]['status'],'candidate')
                # Changing the training PF is still an independent rejection.
                p.coord.min_pf=2
                self.assertEqual(p._forced_data()['rows'],[])
                p._forced_read_at=0
                blob['connection']='bingx-x01';path.write_text(json.dumps(blob))
                self.assertEqual(p._forced_data(),{})

    def test_forced_paths_and_worker_options_are_separate(self):
        self.assertNotEqual(hc.forced_path('bingx-x01'),hc.forced_path('bingx-x02'))
        self.assertEqual(hc.parse_options({})['controlMinTrades'],0)
        self.assertEqual(hc.parse_options({'controlMinTrades':0})['controlMinTrades'],0)
        self.assertEqual(hc.overlay_from_options({'hours':24,'controlMinTrades':5})['controlMinTrades'],5)
        self.assertEqual(hc.overlay_from_options({'hours':24,'controlMinTrades':5},{'controlMinTrades':0})['controlMinTrades'],0)


if __name__=='__main__':unittest.main()
