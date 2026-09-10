import concurrent.futures, pathlib, sys, unittest
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'server/pulse'))
import pulse_trader as pt
from runtime_scope import row_scope_matches, tracking_scope

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

    def test_scope_identity_rejects_cross_lane_and_symbol_inference(self):
        own = {"system_id": "cts-g", "connection": "bingx-x01", "tracking_scope": tracking_scope("bingx-x01"), "client_id": "Gx01oabc"}
        foreign = {**own, "tracking_scope": tracking_scope("bingx-x02"), "client_id": "Gx02oabc"}
        untagged = {"symbol": "SOL-USDT", "qty": 1}
        self.assertTrue(row_scope_matches(own, "bingx-x01"))
        self.assertFalse(row_scope_matches(foreign, "bingx-x01"))
        self.assertFalse(row_scope_matches(untagged, "bingx-x01"))

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

    def test_large_indices_and_independent_tokens_roundtrip(self):
        p = self.pulse()
        for idx in (1000, 34319, 50000):
            st = SimpleNamespace(id=f'general:1m:sl0.6:st30:{idx}', pack='general',
                                 sl_ratio=.6, trail_key='', step=30, idx=idx)
            p.sets.get_idx = lambda index: st if index == idx else None
            for tag in ('Gx02', 'G123456x02'):
                for independent in (False, True):
                    pos = SimpleNamespace(set_id=st.id, pack='general', set_idx=idx,
                        execution_lane='variant' if independent else '',
                        control_group_key='lane:independent' if independent else 'v1:abcdefgh',
                        control_range_key='sl0048-tp0075')
                    with patch.object(pt, 'TAG', tag):
                        ids = [p.cid('u', pos=pos) for _ in range(100)]
                        self.assertEqual(len(set(ids)), 100)
                        for cid in ids:
                            parsed = p.parse_track(cid)
                            self.assertEqual(len(cid), 32)
                            self.assertEqual(parsed['idx'], idx)
                            self.assertEqual(parsed['set_id'], st.id)
                            self.assertEqual(parsed['group_token'], pt.control_group_token(pos.control_group_key, pos.control_range_key))

if __name__ == '__main__': unittest.main()
