import concurrent.futures, pathlib, sys, unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt

class ClientOrderIds(unittest.TestCase):
    def pulse(self):
        p = pt.Pulse.__new__(pt.Pulse)
        p.per_config_controls = lambda pos=None: True
        p.sets = SimpleNamespace(get_idx=lambda idx: None)
        return p

    def test_parallel_grouped_ids_and_parser_on_both_installations(self):
        p = self.pulse()
        pos = SimpleNamespace(set_id='general:1m:sl0.6:tr0.3:st8', pack='general',
            set_idx=0, control_group_key='v1:abcdefgh', control_range_key='sl0048-tp0075')
        for tag in ('Gx02', 'G123456x02'):
            with patch.object(pt, 'TAG', tag):
                with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                    ids = list(pool.map(lambda _: p.cid('c', pos=pos), range(5000)))
                self.assertEqual(len(set(ids)), 5000)
                self.assertTrue(all(len(cid) == 32 for cid in ids))
                self.assertTrue(all(p.parse_track(cid)['group_token'] == 'r048075' for cid in ids))

    def test_exhaustion_fails_before_reuse(self):
        prefix = 'unit-exhaustion'
        with patch.dict(pt._CID_SEQUENCES, {prefix: (35, 0)}):
            values = [pt.client_order_nonce(prefix, 1) for _ in range(36)]
            self.assertEqual(len(set(values)), 36)
            with self.assertRaises(RuntimeError): pt.client_order_nonce(prefix, 1)

    def test_ungrouped_tail_stays_legacy_compatible(self):
        p = self.pulse()
        cid = p.cid('o')
        self.assertEqual(p.parse_track(cid)['group_token'], '')

if __name__ == '__main__': unittest.main()
