"""Regression tests for the calculation defects found by the results/system audit.

PF normal is the classic profit factor after PositionCost; the cost PF ratio is
the stage-gate scale. Wins/losses, PF, drawdown time and every export must
describe one tape at one cost. The cases below fail on the code before the fix.
"""
import io
import json
import os
import pathlib
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'server/pulse'))
import combo_eval  # noqa: E402
import position_cost as pc  # noqa: E402
import pulse_http as ph  # noqa: E402
import set_engine as se  # noqa: E402
import stats_report as sr  # noqa: E402


def strict_loads(raw):
    def refuse(name):
        raise ValueError('non-standard JSON constant ' + name)
    return json.loads(raw, parse_constant=refuse)


class CostBoundary(unittest.TestCase):
    def test_the_desk_slider_minimum_is_a_percent_not_a_fraction(self):
        self.assertAlmostEqual(pc.cost_as_frac(0.02), 0.0002)
        self.assertAlmostEqual(pc.cost_as_frac(0.019), 0.019)  # legacy fraction convention stays below the slider minimum
        rows = [{'t': i, 'pnl_pct': 0.003} for i in range(15)]
        m = pc.last_n_cost_pf(rows, 15, 0.02)
        self.assertGreater(m['classicPf'], 1.0)
        self.assertGreater(m['netAvg'], 0.0)
        self.assertLess(m['ratio'], 10.0)  # 100x too large before the fix (ratio 2.4 at avgR 14 came from a 2% cost)


class DrawdownUsesTheConfiguredCost(unittest.TestCase):
    def score(self, cost_pct):
        book = se.SetBook()
        book.load({'stratGeneral': True, 'stratIndications': False, 'stratTrailing': False,
                   'slToTpRatios': [.4], 'setMinStep': 3, 'setStepMax': 3, 'baseEvalPosCount': 30})
        book.cost_pct = cost_pct
        # gross +0.25% every minute: all winners at cost 0.10, all losers at cost 0.30
        tape = [dict(t=1000 + i * 60, symbol='X-USDT', side='LONG', pnl_pct=0.0025, hold_s=60) for i in range(40)]
        return book._fast_historic_metrics(tape, hist_n=len(tape))

    def test_pf_and_ddt_gate_on_the_same_cost(self):
        cheap, dear = self.score(0.10), self.score(0.30)
        self.assertEqual(cheap['max_dd_s'], 0.0)
        self.assertEqual(dear['wr'], 0.0)
        self.assertGreater(dear['max_dd_s'], 0.0, 'every trade loses after cost, so there must be a drawdown')
        self.assertGreaterEqual(dear['dd_episodes'], 1)

    def test_a_measured_zero_cost_is_a_value_not_a_missing_one(self):
        row = {'t': 1, 'symbol': 'A', 'pnl_pct': 0.0005, 'position_cost_pct': 0.0, 'cost_source': 'live-exchange'}
        self.assertAlmostEqual(pc.row_net_pnl(row, 0.10), 0.0005)
        self.assertAlmostEqual(se.row_equity_pnl(row, 0.10), 0.0005)


class WinsAndLossesAreNetOfCost(unittest.TestCase):
    def test_combo_win_rate_matches_the_net_pf_beside_it(self):
        fills = [{'t': i, 'pnl_pct': 0.0005, 'pack': 'general', 'set_id': 'g:1', 'hold_s': 30, 'reason': 'tp'} for i in range(20)]
        overall = combo_eval.evaluate_fills(fills, cost_pct=0.10, pf_n=15)['pfStats']['overall']
        self.assertLess(overall['netAvg'], 0.0)
        self.assertLess(overall['pf'], 1.0)
        self.assertEqual(overall['wr'], 0.0)

    def report(self):
        closed, t = [], 1000
        for _ in range(20):  # gross +0.05% against a 0.10% cost: net losers
            t += 60
            closed.append({'t': t, 'symbol': 'AAA-USDT', 'side': 'LONG', 'qty': 1.0, 'entry': 100.0, 'exit': 100.05,
                           'pnl': 100.0 * (0.0005 - 0.001), 'pnl_pct': 0.0005, 'hold_s': 30, 'reason': 'tp'})
        for _ in range(2):
            t += 60
            closed.append({'t': t, 'symbol': 'AAA-USDT', 'side': 'LONG', 'qty': 1.0, 'entry': 100.0, 'exit': 99.5,
                           'pnl': 100.0 * (-0.005 - 0.001), 'pnl_pct': -0.005, 'hold_s': 30, 'reason': 'sl'})
        return sr.build({'closed': closed, 'sets': {'rows': []}, 'open': [], 'pfCost': {'n': 15}}, cost_pct=0.10, conn='x02')

    def test_exported_profit_factor_windows_count_wins_after_cost(self):
        window = self.report()['profitFactor']['all']
        self.assertEqual(window['wins'], 0)
        self.assertEqual(window['losses'], 22)
        self.assertEqual(window['wr'], 0.0)
        self.assertLess(window['classicPf'], 1.0, 'classicPf is PF normal: after PositionCost')
        self.assertGreater(window['grossClassicPf'], window['classicPf'])

    def test_markdown_names_the_net_and_the_gross_pf_apart(self):
        text = sr.render_md(self.report())
        self.assertIn('classic PF=', text)
        self.assertIn('gross classic PF=', text)

    def test_legacy_rows_without_pnl_pct_are_not_read_as_cost_only_losses(self):
        rows = [{'t': 1000 + i * 60, 'symbol': 'AAA-USDT', 'side': 'LONG', 'qty': 1.0, 'entry': 100.0, 'pnl': 1.0,
                 'hold_s': 30, 'reason': 'tp'} for i in range(15)]
        blob = sr.build({'closed': rows, 'sets': {'rows': []}, 'open': [], 'pfCost': {'n': 15}}, cost_pct=0.10, conn='x02')
        last15 = blob['costAccounting']['last15']
        self.assertGreater(last15['ratio'], 1.0)
        self.assertGreater(last15['avgR'], 0.0)


class OverallMatchesTheLanes(unittest.TestCase):
    NOW = 1_800_000_000.0

    def lane(self, lane_id, scope, wins, losses, cost, mu, dd, hist_fills, set_count, active, rng):
        closed = []
        for i in range(80):
            gross = rng.gauss(mu, 0.004)
            closed.append({'t': self.NOW - 60 - i * 120.0, 'symbol': rng.choice(['AAA-USDT', 'BBB-USDT']),
                           'side': rng.choice(['LONG', 'SHORT']), 'qty': 1.0, 'entry': 50.0, 'exit': 50.0,
                           'pnl': 50.0 * (gross - cost / 100.0), 'pnl_pct': gross, 'hold_s': 60, 'reason': 'tp',
                           'pack': 'general', 'systemId': 'cts-g', 'trackingScope': scope, 'connection': lane_id,
                           'clientId': 'Gx0-' + str(i), 'position_cost_pct': cost})
        pnls = [c['pnl'] for c in closed]
        cost_pf = pc.last_n_cost_pf(list(reversed(closed)), 15, cost)
        cost_pf.update(minPf=1.15, requiredSamples=15, pass_=False)
        return {'running': True, 'halted': False, 'systemId': 'cts-g', 'trackingScope': scope, 'connection': lane_id,
                'trackPrefix': 'Gx0-', 'equity': 100.0, 'systemEquity': 100.0, 'available': 50.0, 'usedMargin': 25.0,
                'systemPnl': sum(pnls), 'sessionPnl': sum(pnls), 'systemGrow': sum(p for p in pnls if p > 0),
                'systemLoss': abs(sum(p for p in pnls if p < 0)), 'wins': wins, 'losses': losses,
                'winRate': round(100.0 * wins / (wins + losses), 1), 'drawdownPct': dd, 'openCount': 0, 'open': [],
                'closed': closed, 'pfCost': cost_pf, 'tradedNotional': 5000.0, 'symbols': ['AAA-USDT', 'BBB-USDT'],
                'sets': {'setCount': set_count, 'activeCount': active, 'validatedCount': active * 2,
                         'histFills': hist_fills, 'internSetCount': set_count, 'catalogSetCount': set_count,
                         'rows': [], 'liveFills': 3},
                'coord': {'axes': {}}, 'coverage': {}, 'activity': {}}

    def merged(self):
        rng = random.Random(5)
        live = self.lane('bingx-x01', 'cts-g:bingx-x01', 300, 200, 0.10, 0.0015, 3.0, 5000, 100, 10, rng)
        vst = self.lane('bingx-x02', 'cts-g:bingx-x02', 10, 90, 0.04, -0.001, 20.0, 90000, 400, 40, rng)
        store = {'bingx-x01': live, 'bingx-x02': vst}
        with patch.object(ph, 'load_stats', lambda cid: store[cid]), \
                patch.object(ph, 'unit_state', lambda cid, fresh=False: 'active'), \
                patch.object(ph, 'stats_age', lambda cid: 1.0):
            return ph.merge_overall(), ph.overall_report_state(live, vst)

    def test_overall_set_counters_are_summed_over_the_same_lanes(self):
        out, _ = self.merged()
        self.assertEqual(out['sets']['histFills'], 95000)
        self.assertEqual(out['sets']['liveFills'], 6)

    def test_overall_export_counts_the_lanes_persistent_wins_and_losses(self):
        out, state = self.merged()
        self.assertEqual((out['wins'], out['losses']), (310, 290))
        self.assertEqual((state['wins'], state['losses']), (310, 290))
        self.assertAlmostEqual(state['winRate'], 51.7, places=1)


class NoBareNanInJson(unittest.TestCase):
    def test_results_export_is_strict_json(self):
        rows = [{'t': 1_800_000_000 - i, 'symbol': 'A-USDT', 'side': 'LONG', 'qty': 1, 'entry': 100, 'pnl': 1.0,
                 'pnl_pct': (1e308 if i % 2 == 0 else -1e308), 'hold_s': 1, 'reason': 'tp'} for i in range(20)]
        with tempfile.TemporaryDirectory() as tmp:
            sr.write({'closed': rows, 'sets': {'rows': []}, 'open': []}, os.path.join(tmp, 'r.json'),
                     os.path.join(tmp, 'r.md'), cost_pct=0.1, conn='x02')
            with open(os.path.join(tmp, 'r.json')) as handle:
                strict_loads(handle.read())

    def test_sidecar_response_is_strict_json(self):
        class Dummy:
            def __init__(self):
                self.wfile = io.BytesIO()

            def send_response(self, code):
                pass

            def send_header(self, *args):
                pass

            def end_headers(self):
                pass

            def _cors(self):
                pass

        dummy = Dummy()
        ph.Handler._json(dummy, {'pfCost': {'avgR': float('nan'), 'classicPf': float('inf')}, 'ok': 1.5})
        body = strict_loads(dummy.wfile.getvalue().decode())
        self.assertEqual(body['ok'], 1.5)
        self.assertIsNone(body['pfCost']['avgR'])


if __name__ == '__main__':
    unittest.main()
