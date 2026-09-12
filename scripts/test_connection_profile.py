"""Identical processing profiles, isolated mainnet/VST endpoints and books."""
import os
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'/'pulse'))
from connection_profile import connection_endpoint, processing_profile
from prepare_connection_profile import prepare
from runtime_scope import order_tag, tracking_scope, row_scope_matches


class ConnectionProfileTests(unittest.TestCase):
    def test_both_connections_use_exactly_the_same_settings_patch(self):
        live = prepare('bingx-x01', {'minPf':1.26, 'setStrictGate':False, 'api_key':'not-copied'})
        demo = prepare('bingx-x02')
        self.assertEqual(live['profilePatch'], demo['profilePatch'])
        self.assertEqual(live['profilePatch'], processing_profile())
        self.assertNotIn('api_key', str(live))
        self.assertFalse(live['activationPerformed'])
        self.assertEqual(live['changes']['minPf'], {'previous':1.26, 'proposed':1.05})

    def test_profile_defaults_and_independent_returns(self):
        p = processing_profile()
        for key in ('minPf','baseMinPf','mainMinPf','realMinPf','setMinPf','dcaMinPf','exitMinPf'):
            self.assertEqual(p[key],1.05)
        for key in ('maxOpen','maxPerGroup','setMaxActive','entryPolicyMaxCandidates'):
            self.assertEqual(p[key],0)
        self.assertEqual(p['histLookbackBars'],2880)
        self.assertEqual(p['baseEvalPosCount'],30)
        self.assertEqual(p['symbolCap'],20)
        self.assertTrue(p['controlOrdersPerConfig'])
        p['minPf']=999
        self.assertEqual(processing_profile()['minPf'],1.05)

    def test_endpoints_default_to_their_own_connection(self):
        self.assertEqual(connection_endpoint('bingx-x01'),'https://open-api.bingx.com')
        self.assertEqual(connection_endpoint('bingx-x02'),'https://open-api-vst.bingx.com')

    def test_cross_connection_endpoints_and_vst_only_mismatch_are_rejected(self):
        with self.assertRaises(ValueError):connection_endpoint('bingx-x01','https://open-api-vst.bingx.com')
        with self.assertRaises(ValueError):connection_endpoint('bingx-x02','https://open-api.bingx.com')
        with self.assertRaises(ValueError):connection_endpoint('bingx-x01',is_testnet='true')
        with self.assertRaises(ValueError):connection_endpoint('bingx-x01',vst_only=True)
        with self.assertRaises(ValueError):connection_endpoint('unknown')

    def test_credentials_and_parameters_in_endpoint_are_rejected(self):
        for base in ('http://open-api.bingx.com','https://key@open-api.bingx.com',
                     'https://open-api.bingx.com/path','https://open-api.bingx.com?key=secret',
                     'https://open-api.bingx.com:444','https://open-api.bingx.com#fragment'):
            with self.subTest(base=base), self.assertRaises(ValueError):
                connection_endpoint('bingx-x01',base)

    def test_shared_structures_never_share_ownership(self):
        self.assertNotEqual(order_tag('bingx-x01'),order_tag('bingx-x02'))
        self.assertNotEqual(tracking_scope('bingx-x01'),tracking_scope('bingx-x02'))
        row = {'tracking_scope':tracking_scope('bingx-x02')}
        self.assertFalse(row_scope_matches(row,'bingx-x01'))
        self.assertTrue(row_scope_matches(row,'bingx-x02'))

    def test_mainnet_700_set_pipeline_with_fake_exchange_and_rate_limit(self):
        env = dict(os.environ, PULSE_CONN='bingx-x01')
        result = subprocess.run([sys.executable,'-m','unittest',
            'test_continuous_real_live.ContinuousTests.test_700_qualified_sets_resume_after_rate_limit_open_once_and_keep_controls'],
            cwd=ROOT/'scripts', env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':unittest.main()
