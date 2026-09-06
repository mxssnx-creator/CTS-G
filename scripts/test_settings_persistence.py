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
            self.assertEqual(vst,{f'field{i}':i for i in range(60)})
            self.assertEqual(live,{'sentinel':'x01'})
            self.assertFalse(list(pathlib.Path(d).glob('*.tmp')))
    def test_nonfinite_or_nonobject_cannot_overwrite_saved_settings(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            ph.write_overlay('vst',{'volumeFactor':.1})
            for bad in ({'volumeFactor':float('nan')},{'nested':[float('inf')]},[]):
                with self.assertRaises(ValueError):ph.write_overlay('vst',bad)
                self.assertEqual(ph.load_overlay('bingx-x02'),{'volumeFactor':.1})
    def test_execution_settings_roundtrip_without_disabling_calculation(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            ph.write_overlay('vst',{'normalExecutionEnabled':False,'blockActive':True,'setMaxActive':50,'stratGeneral':True})
            ph.write_overlay('vst',{'blockActive':False})
            value=ph.load_overlay('bingx-x02')
            self.assertEqual(value,{'normalExecutionEnabled':False,'blockActive':False,'setMaxActive':50,'stratGeneral':True})
            ph.write_overlay('vst',{'normalExecutionEnabled':True})
            self.assertTrue(ph.load_overlay('bingx-x02')['normalExecutionEnabled'])
    def test_invalid_lane_cannot_write_a_file(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            with self.assertRaises(ValueError):ph.write_overlay('../../foreign',{'x':1})
            self.assertEqual(list(pathlib.Path(d).iterdir()),[])

if __name__=='__main__':unittest.main()
