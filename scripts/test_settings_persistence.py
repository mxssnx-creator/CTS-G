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
            self.assertEqual({k:value[k] for k in ('normalExecutionEnabled','setMaxActive','stratGeneral')},{'normalExecutionEnabled':False,'setMaxActive':50,'stratGeneral':True})
            self.assertTrue(value['blockActive'])
            self.assertTrue(value['blockEnabled'])
            ph.write_overlay('vst',{'normalExecutionEnabled':True})
            self.assertTrue(ph.load_overlay('bingx-x02')['normalExecutionEnabled'])
    def test_invalid_lane_cannot_write_a_file(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            with self.assertRaises(ValueError):ph.write_overlay('../../foreign',{'x':1})
            self.assertEqual(list(pathlib.Path(d).iterdir()),[])
    def test_historic_test_hours_and_min_pf_persist_independently_of_lookback(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            ph.write_overlay('vst',{'histTestHours':48,'histTestMinPf':1.2,'histLookbackBars':2880})
            value=ph.load_overlay('bingx-x02')
            self.assertEqual(value['histTestHours'],48)
            self.assertAlmostEqual(value['histTestMinPf'],1.2)
            self.assertEqual(value['histLookbackBars'],2880)
            ph.write_overlay('vst',{'histTestHours':99,'histTestMinPf':0.5})
            value=ph.load_overlay('bingx-x02')
            self.assertEqual(value['histTestHours'],64)
            self.assertAlmostEqual(value['histTestMinPf'],1.02)
            self.assertEqual(value['histLookbackBars'],2880)
            self.assertTrue(value.get('histTestEnabled',True))
            ph.write_overlay('vst',{'histTestEnabled':False})
            self.assertFalse(ph.load_overlay('bingx-x02')['histTestEnabled'])
            ph.write_overlay('live',{'histTestHours':8,'histTestMinPf':1.1})
            live=ph.load_overlay('bingx-x01')
            self.assertEqual(live['histTestHours'],8)
            self.assertNotEqual(ph.load_overlay('bingx-x02')['histTestHours'],8)
    def test_sqlite_modes_and_checkpoints_persist_independently_per_lane(self):
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            ph.write_overlay('vst',{'systemSqliteMemory':1,'systemSqliteCheckpointS':3})
            ph.write_overlay('live',{'systemSqliteMemory':0,'systemSqliteCheckpointS':25})
            for lane,mode,seconds in (('bingx-x02',1,3),('bingx-x01',0,25)):
                value=ph.load_overlay(lane)
                self.assertEqual(value['systemSqliteMemory'],mode)
                self.assertEqual(value['systemSqliteCheckpointS'],seconds)
                self.assertTrue(value['stratGeneral'])

    def test_operator_symbol_and_toggle_choices_are_honored(self):
        with tempfile.TemporaryDirectory() as d, patch.object(ph, 'DIR', d):
            ph.write_overlay('live', {'symbols': ['*'], 'symbolCap': 50, 'blockEnabled': True, 'histTestEnabled': True})
            ph.write_overlay('live', {'symbols': ['HYPE-USDT', 'kas-usdt', 'bad name', 'HYPE-USDT'], 'symbolsAll': False, 'symbolCap': 3,
                                      'blockEnabled': False, 'blockMaxStack': 2, 'dcaEnabled': False, 'stratDca': False})
            live = ph.load_overlay('bingx-x01')
            self.assertEqual(live['symbols'], ['HYPE-USDT', 'KAS-USDT'])
            self.assertFalse(live['symbolsAll'])
            self.assertEqual(live['symbolCap'], 3)
            self.assertEqual(live['blockMaxStack'], 2)
            self.assertFalse(live['dcaEnabled'])
            self.assertFalse(live['stratDca'])
            self.assertTrue(live['blockEnabled'])
            self.assertTrue(live['blockOverall'])
            ph.write_overlay('live', {'symbolCap': 0, 'blockMaxStack': 99})
            live = ph.load_overlay('bingx-x01')
            self.assertEqual(live['symbolCap'], 0)
            self.assertEqual(live['blockMaxStack'], 6)
            ph.write_overlay('vst', {'symbols': ['FLYBRAIN-USDT'], 'symbolCap': 5, 'histTestEnabled': False})
            vst = ph.load_overlay('bingx-x02')
            self.assertEqual(vst['symbols'], ['FLYBRAIN-USDT'])
            self.assertEqual(vst['symbolCap'], 5)
            self.assertFalse(vst['histTestEnabled'])
            ph.write_overlay('vst', {'symbols': ['not a symbol']})
            self.assertEqual(ph.load_overlay('bingx-x02')['symbols'][:4], ['BTC-USDT', 'ETH-USDT', 'SOL-USDT', 'XRP-USDT'])

    def test_absent_keys_keep_lane_defaults(self):
        with tempfile.TemporaryDirectory() as d, patch.object(ph, 'DIR', d):
            out = ph.guard_runtime_overlay('bingx-x02', {})
            self.assertTrue(out['dcaEnabled'] and out['stratDca'] and out['histTestEnabled'])
            self.assertEqual(out['symbolCap'], 50)
            self.assertEqual(out['blockMaxStack'], 6)

    def test_min_sl_floor_is_systemwide_point_four(self):
        with tempfile.TemporaryDirectory() as d, patch.object(ph, 'DIR', d):
            ph.write_overlay('live', {'slMinPct': 0.15, 'indStopMinPct': 0.2, 'slPct': 0.2})
            live = ph.load_overlay('bingx-x01')
            self.assertGreaterEqual(float(live['slMinPct']), 0.4)
            self.assertGreaterEqual(float(live['indStopMinPct']), 0.4)
            self.assertGreaterEqual(float(live['slPct']), float(live['slMinPct']))
            ph.write_overlay('vst', {'slMinPct': 0.4, 'indStopMinPct': 0.4})
            vst = ph.load_overlay('bingx-x02')
            self.assertAlmostEqual(float(vst['slMinPct']), 0.4)
            self.assertAlmostEqual(float(vst['indStopMinPct']), 0.4)

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


ROOT=pathlib.Path(__file__).resolve().parents[1]
LANE_FILES={'bingx-x01':ROOT/'server/pulse/overlay-bingx-x01.json','bingx-x02':ROOT/'server/pulse/overlay-bingx-x02.json'}


class SettingsContract(unittest.TestCase):
    """Desk settings mean the same thing to every engine reader."""

    def test_indications_pack_switch_gates_live_indication_entries(self):
        import modules
        on={'indEnabled':True,'stratIndications':True,'modules':{'strategy.indications':True}}
        self.assertTrue(modules.resolve(on)['strategy.indications'])
        self.assertFalse(modules.resolve({**on,'stratIndications':False})['strategy.indications'])
        self.assertFalse(modules.resolve({**on,'indEnabled':False})['strategy.indications'])
        self.assertTrue(modules.resolve({'modules':{}})['strategy.indications'])

    def test_percent_settings_have_one_unit_inside_the_desk_range(self):
        import pulse_trader as pt
        from exit_engine import ExitBook
        from dca_engine import DcaBook
        for pct in (1,2,5,25,80):
            self.assertAlmostEqual(pt.drawdown_halt_fraction(pct),pct/100)
        self.assertEqual(pt.drawdown_halt_fraction(0),0.0)
        for pct in (0.01,0.02,0.03,0.04,0.2):
            book=ExitBook();book.load({'exitBeBuffer':pct})
            self.assertAlmostEqual(book.be_buffer,pct/100)
        for pct in (0.05,0.1,0.2,1.0):
            dca=DcaBook();dca.load({'dcaBreakevenProfitPct':pct})
            self.assertAlmostEqual(dca.be_pct,pct/100)

    def test_noise_is_a_percent_for_coordination_and_indications(self):
        from coord_engine import Coordinator
        from indication_engine import DEFAULT_SETTINGS, IndicationBook, evaluate_break
        coord=Coordinator();coord.load({},{'noise':0.05})
        bar=lambda span:[100.0,100.0+span/2,100.0-span/2,100.0,1.0]
        self.assertTrue(coord.outbreak_ok([bar(0.1)]*12))      # 0.1% range clears 0.05%
        self.assertFalse(coord.outbreak_ok([bar(0.008)]*12))   # 0.008% does not
        closes=[100.0]*19+[100.5]
        for noise in (0.02,0.03,0.05):  # 0.5% break clears every desk noise value
            # Classic break fixture (reversal-mode noise is covered in test_indication_break).
            settings={**DEFAULT_SETTINGS,'breakRange':16,'activeNoise':noise,'breakContextSigma':0}
            self.assertIsNotNone(evaluate_break('X-USDT',closes,settings),noise)
        book=IndicationBook();book.load({'noise':0.02})
        self.assertEqual(book.settings['activeNoise'],0.02)

    def test_stored_overlays_keep_their_effective_percent_values(self):
        import pulse_trader as pt
        from exit_engine import ExitBook
        from dca_engine import DcaBook
        from indication_engine import IndicationBook
        for lane,path in LANE_FILES.items():
            ov=json.loads(path.read_text())
            self.assertEqual(pt.drawdown_halt_fraction(ov['drawdownHaltPct']),0.0,lane)
            book=ExitBook();book.load(ov);self.assertAlmostEqual(book.be_buffer,0.0004)
            dca=DcaBook();dca.load(ov);self.assertAlmostEqual(dca.be_pct,0.002)
            ind=IndicationBook();ind.load(ov);self.assertEqual(ind.settings['activeNoise'],0.05)

    def test_explicit_zero_is_honored(self):
        from block_engine import BlockBook, clamp_pause_count_ratio
        from coord_engine import Coordinator
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(BlockBook(str(pathlib.Path(d)/'a.json'),{'blockPauseCountRatio':0}).pause_ratio,0)
            self.assertEqual(BlockBook(str(pathlib.Path(d)/'b.json'),{}).pause_ratio,1)
        self.assertEqual(clamp_pause_count_ratio(0),0)
        self.assertEqual(clamp_pause_count_ratio(None),1)
        self.assertEqual(clamp_pause_count_ratio(3),3)
        coord=Coordinator();coord.load({},{'posCountsVolumeRatio':0})
        self.assertEqual(coord.pos_count_vol_ratio,0.0)
        self.assertEqual(coord.size_mult(40),1.0)
        coord.load({},{})
        self.assertAlmostEqual(coord.pos_count_vol_ratio,0.05)

    def test_preset_load_keeps_each_lane_universe_and_forced_winners(self):
        from user_presets import UserPresetStore
        with tempfile.TemporaryDirectory() as d,patch.object(ph,'DIR',d):
            for lane,path in LANE_FILES.items():
                ph.write_overlay(lane,json.loads(path.read_text()))
            live=ph.load_overlay('bingx-x01');vst=ph.load_overlay('bingx-x02')
            store=UserPresetStore(str(pathlib.Path(d)/'presets.json'),write_overlay=ph.write_overlay,lane_ids=list(LANE_FILES))
            row=store.save({**live,'slToTpRatio':0.9},name='FromLive')
            _,applied=store.apply(row['id'])
            self.assertEqual(applied,list(LANE_FILES))
            after=ph.load_overlay('bingx-x02')
            self.assertEqual(after['slToTpRatio'],0.9)
            for key in ('symbols','symbolsAll','symbolsDynamic','forcedSymbols','forcedVariant','forcedEligible','forcedBest'):
                self.assertEqual(after.get(key),vst.get(key),key)
            self.assertEqual(ph.load_overlay('bingx-x01')['symbols'],live['symbols'])

if __name__=='__main__':unittest.main()
