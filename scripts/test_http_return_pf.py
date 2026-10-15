"""HTTP lane headlines use own return evidence rather than cash win/loss PF."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_http as http

LANE = dict(id='bingx-x02', type='vst', label='VST', unit='USDT', exchange='BingX')


class HttpReturnPfTests(unittest.TestCase):
    def summary(self, rows, **policy):
        state = dict(trackingScope='cts-g:bingx-x02',
                     closed=[dict(row, ours=True, trackingScope='cts-g:bingx-x02') for row in rows],
                     pfCost=dict(n=5, costPct=.1, **policy))
        with patch.object(http, 'unit_state', return_value='active'), \
                patch.object(http, 'stats_age', return_value=0):
            return http.lane_summary(LANE, state)

    def test_headline_is_return_pf_and_classic_is_named_diagnostic(self):
        result = self.summary([dict(t=1, pnl=100, pnl_pct=.002)])
        self.assertAlmostEqual(result['pf'], 1.1)
        self.assertEqual(result['pfCost'], result['pf'])
        self.assertEqual(result['classicCashPf'], 99)
        self.assertEqual(result['pfSampleCount'], 1)

    def test_unknown_roi_cannot_create_a_positive_lane(self):
        result = self.summary([dict(t=1, pnl=100)])
        self.assertIsNone(result['pf'])
        self.assertIsNone(result['pfCost'])
        self.assertEqual(result['pfSampleCount'], 0)

    def test_measured_zero_cost_preserves_configured_reference(self):
        result = self.summary([dict(t=1, pnl=.2, pnl_pct=.002, qty=1,
            entry=100, fee_total=0, cost_source='live-exchange')], pfReferenceCostPct=.2)
        self.assertAlmostEqual(result['pf'], 1.1)


if __name__ == '__main__':
    unittest.main()
