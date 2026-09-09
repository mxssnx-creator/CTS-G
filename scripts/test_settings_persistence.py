"""Parallel settings updates, invalid JSON numbers, and connection isolation."""
import json, pathlib, sys, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server/pulse'))
import pulse_http as ph

class SettingsPersistence(unittest.TestCase):
    def test_concurrent_partial_updates_survive_and_lanes_stay_independent(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(lambda i:ph.write_overlay('vst',{f'field{i}':i}),range(60)))
            ph.write_overlay('live',{'sentinel':'x01'})
            vst=ph.load_overlay('bingx-x02');live=ph.load_overlay('bingx-x01')
            self.assertEqual({k:v for k,v in vst.items() if k.startswith('field')},{f'field{i}':i for i in range(60)})
            self.assertEqual(live['sentinel'],'x01')
            self.assertFalse(any(k.startswith('field') for k in live))
            self.assertTrue(vst['stratGeneral'])
            self.assertFalse(list(pathlib.Path(d).glob('*.tmp')))
    def test_nonfinite_or_nonobject_cannot_overwrite_saved_settings(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            ph.write_overlay('vst',{'volumeFactor':.1})
            saved=ph.load_overlay('bingx-x02')
            for bad in ({'volumeFactor':float('nan')},{'nested':[float('inf')]},[]):
                with self.assertRaises(ValueError):ph.write_overlay('vst',bad)
                self.assertEqual(ph.load_overlay('bingx-x02'),saved)
    def test_execution_settings_roundtrip_without_disabling_calculation(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            ph.write_overlay('vst',{'normalExecutionEnabled':False,'blockActive':True,'setMaxActive':50,'stratGeneral':True})
            ph.write_overlay('vst',{'blockActive':False})
            value=ph.load_overlay('bingx-x02')
            self.assertEqual({k:value[k] for k in ('normalExecutionEnabled','blockActive','setMaxActive','stratGeneral')},{'normalExecutionEnabled':False,'blockActive':False,'setMaxActive':50,'stratGeneral':True})
            ph.write_overlay('vst',{'normalExecutionEnabled':True})
            self.assertTrue(ph.load_overlay('bingx-x02')['normalExecutionEnabled'])
    def test_invalid_lane_cannot_write_a_file(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            with self.assertRaises(ValueError):ph.write_overlay('../../foreign',{'x':1})
            self.assertEqual(list(pathlib.Path(d).iterdir()),[])
    def test_sqlite_modes_and_checkpoints_persist_independently_per_lane(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            ph.write_overlay('vst',{'systemSqliteMemory':1,'systemSqliteCheckpointS':3})
            ph.write_overlay('live',{'systemSqliteMemory':0,'systemSqliteCheckpointS':25})
            for lane,mode,seconds in (('bingx-x02',1,3),('bingx-x01',0,25)):
                value=ph.load_overlay(lane)
                self.assertEqual(value['systemSqliteMemory'],mode)
                self.assertEqual(value['systemSqliteCheckpointS'],seconds)
                self.assertTrue(value['stratGeneral'])

    def test_overall_start_reports_a_failed_lane(self):
        def service_state(cid, fresh=False):
            return 'active' if cid == 'bingx-x01' else 'failed'

        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(ph, 'DIR', d),
            patch.object(ph, 'STOP_ALL_PATH', str(pathlib.Path(d) / 'STOP')),
            patch.object(ph, '_sysctl', return_value=(0, 'ok')),
            patch.object(ph, 'unit_state', side_effect=service_state),
            patch.object(ph, '_live_start_allowed', return_value=True),
        ):
            ok, detail = ph.apply_control('overall', 'start')
            self.assertFalse(ok)
            self.assertIn('bingx-x01', detail)
            self.assertIn('bingx-x02', detail)

    def test_crashed_service_overrides_stale_running_snapshot_and_progress(self):
        with (
            tempfile.TemporaryDirectory() as d,
            patch.object(ph, 'DIR', d),
            patch.object(ph, 'STOP_ALL_PATH', str(pathlib.Path(d) / 'STOP')),
            patch.object(ph, 'unit_state', return_value='failed'),
        ):
            path = pathlib.Path(ph.stats_path('bingx-x01'))
            path.write_text('{}')
            out = ph.stamp_stats({
                'running': True,
                'halted': False,
                'progressPhase': 'replay',
                'progressPct': 42,
                'progressReady': True,
            }, 'bingx-x01')
            self.assertFalse(out['running'])
            self.assertTrue(out['stale'])
            self.assertEqual(out['progressPhase'], 'error')
            self.assertIn('service failed', out['progressDetail'])

if __name__=='__main__':unittest.main()
