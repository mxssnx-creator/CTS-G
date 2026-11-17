"""Block recovery and Auto are isolated by exact Set, lane, relation and count."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server' / 'pulse'))
from block_rounds import RoundsBook, config_of, keys_of


def keys(set_id, relation_lane='pos', symbol='BTC-USDT'):
    return keys_of(symbol, 1, 'trend', 'base', 3, relation_lane, set_id)


class BlockSetScope(unittest.TestCase):
    def config(self, relation='step', auto=False):
        return config_of({'enabled': True, 'relations': [relation], 'maxMult': 4,
                          'auto': auto, 'autoN': 3, 'autoMin': 1, 'autoPf': 1.0,
                          'counts': {'1': {'on': True, 'ratio': 1.0},
                                     '2': {'on': True, 'ratio': .5}}})

    @staticmethod
    def trade(book, key, pid, result):
        mult = book.ask(key)[0]
        book.opened(pid, key, mult)
        book.closed(pid, result)
        return mult

    def test_step_relation_never_borrows_sibling_set_or_lane(self):
        book = RoundsBook(cfg=self.config())
        a, b, negative = keys('trend:L:st3:sl1'), keys('trend:L:st3:sl2'), keys('trend:L:st3:sl1', 'neg')
        self.trade(book, a, 'a1', -1)
        self.assertEqual(book.ask(a)[0], 2)
        self.assertEqual(book.ask(b)[0], 1)
        self.assertEqual(book.ask(negative)[0], 1)
        self.trade(book, a, 'a2', -1)
        self.assertEqual(book.ask(a)[0], 2.5, 'count two is an independent procedure')
        self.assertEqual(book.ask(b)[0], 1)

    def test_selected_relation_can_share_own_set_across_symbols_without_crossing_sets(self):
        book = RoundsBook(cfg=self.config('overall'))
        first = keys('same', symbol='BTC-USDT')
        second = keys('same', symbol='ETH-USDT')
        other = keys('other', symbol='ETH-USDT')
        self.trade(book, first, 'one', -1)
        self.assertEqual(book.ask(second)[0], 2)
        self.assertEqual(book.ask(other)[0], 1)

    def test_missing_or_legacy_scope_cannot_create_a_new_pooled_procedure(self):
        book = RoundsBook(cfg=self.config())
        book.opened('unscoped', {'step': 'trend|3|pos|L'}, 1)
        self.assertEqual(book.ask({'step': 'trend|3|pos|L'}), (1.0, []))
        self.assertFalse(book.groups)
        self.assertFalse(book.members)

    def test_auto_uses_only_that_sets_own_raise_results(self):
        for relation in ('set', 'step', 'overall'):
            with self.subTest(relation=relation):
                cfg = self.config(relation, auto=True)
                cfg.counts[2].on, cfg.counts[2].ratio = False, 0
                book = RoundsBook(cfg=cfg)
                a, b = keys('trend:L:st3:sl1'), keys('trend:L:st3:sl2')
                self.trade(book, a, 'a1', -1)
                self.trade(book, a, 'a2', .5)
                self.trade(book, a, 'a3', -1)
                self.trade(book, b, 'b1', -1)
                self.trade(book, b, 'b2', -.5)
                self.assertGreater(book.ask(a)[0], 1)
                self.assertEqual(book.ask(b)[0], 1, 'a profitable sibling cannot permit a losing Set')

    def test_unknown_close_does_not_turn_a_partial_block_into_a_win(self):
        cfg = self.config()
        cfg.counts[1].on = False
        book = RoundsBook(cfg=cfg)
        a = keys('trend:L:st3:sl1')
        self.trade(book, a, 'one', -1)
        self.trade(book, a, 'unknown', None)
        self.assertEqual(book.ask(a)[0], 1, 'an unscored block cannot start a recovery')
        self.trade(book, a, 'three', -1)
        self.trade(book, a, 'nonfinite', float('inf'))
        self.assertEqual(book.ask(a)[0], 1, 'a nonfinite return cannot score a block')

    def test_unknown_auto_result_is_not_replaced_by_older_wins(self):
        cfg = self.config('set', auto=True)
        cfg.auto_n = 2
        cfg.counts[2].on, cfg.counts[2].ratio = False, 0
        book = RoundsBook(cfg=cfg)
        a = keys('same')
        self.trade(book, a, 'initial-loss', -1)
        self.trade(book, a, 'unknown-raise', None)
        self.assertEqual(book.ask(a)[0], 1)
        row = next(r for r in book.snapshot()['auto'] if r['count'] == 1)
        self.assertEqual((row['source'], row['pf']), ('unknown', None))
        self.trade(book, a, 'observed-one', .2)
        self.assertEqual(book.ask(a)[0], 1)
        self.trade(book, a, 'observed-two', .2)
        self.assertEqual(book.ask(a)[0], 2, 'new own observations can age the unknown out')

    def test_restart_preserves_own_scope_and_does_not_import_old_pooled_evidence(self):
        cfg = self.config('set', auto=True)
        cfg.counts[2].on, cfg.counts[2].ratio = False, 0
        a, b = keys('trend:L:st3:sl1'), keys('trend:L:st3:sl2')
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'rounds.json')
            book = RoundsBook(path, cfg)
            self.trade(book, a, 'a1', -1)
            book.save()
            restored = RoundsBook(path, cfg)
            self.assertEqual(restored.ask(a)[0], 2)
            self.assertEqual(restored.ask(b)[0], 1)
            legacy = {'version': 1, 'groups': {}, 'members': {}, 'evidence': {'set\t1': [100, 100]}}
            Path(path).write_text(json.dumps(legacy))
            restored = RoundsBook(path, cfg)
            self.trade(restored, b, 'b1', -1)
            self.trade(restored, b, 'b2', -.5)
            self.assertEqual(restored.ask(b)[0], 1, 'pooled legacy Auto history cannot qualify this Set')

    def test_legacy_open_membership_can_finish_without_infecting_new_scope(self):
        cfg = self.config('step', auto=True)
        legacy_gid = 'step\ttrend|3|pos|L\t1'
        legacy_group = {'level': 1, 'net': -2., 'pause': 0, 'seq': 1, 'blocks': 1, 'lost': 1,
                        'raises': 1, 'recovered': 0, 'last': [],
                        'open': [{'id': 1, 'n': 1, 'closed': 0, 'unit': 0., 'weighted': 0.}]}
        data = {'version': 1, 'groups': {legacy_gid: legacy_group},
                'members': {'old-position': {'mult': 2., 'at': [[legacy_gid, 1, 1]]}},
                'evidence': {'step\t1': [2., 2.]}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'rounds.json';path.write_text(json.dumps(data))
            book = RoundsBook(str(path), cfg)
            book.closed('old-position', .25)
            self.assertEqual(book.ask(keys('new-set'))[0], 1)
            self.assertEqual(book.snapshot()['legacyGroups'], 1)
            book.save()
            self.assertEqual(json.loads(path.read_text())['version'], 3)
            self.assertEqual(RoundsBook(str(path), cfg).ask(keys('new-set'))[0], 1)

    def test_policy_change_keeps_open_members_and_unchanged_counts_in_their_own_histories(self):
        a = keys('same')
        old = self.config('set')
        old.counts[1].pause = 2
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'rounds.json')
            book = RoundsBook(path, old)
            self.trade(book, a, 'first', -1)
            self.trade(book, a, 'second', -1)
            old_id = book.gid('set', a['set'], 1)
            second_id = book.gid('set', a['set'], 2)
            book.opened('owned-by-old-policy', a, book.ask(a)[0])
            book.save()
            new = self.config('set')
            new.counts[1].ratio = .25
            restored = RoundsBook(path, new)
            self.assertEqual(restored.ask(a)[0], 1.5, 'only unchanged count two may raise')
            self.assertEqual(restored.gid('set', a['set'], 2), second_id)
            new_id = restored.gid('set', a['set'], 1)
            self.assertNotEqual(old_id, new_id)
            restored.closed('owned-by-old-policy', 3)
            self.assertEqual(restored.groups[old_id]['pause'], 2)
            self.assertNotIn(new_id, restored.groups)
            self.assertEqual(restored.snapshot()['inactivePolicyGroups'], 1)
            restored.cfg = old
            self.assertEqual(restored.groups[restored.gid('set', a['set'], 1)]['pause'], 2)

    def test_independent_recovery_ignores_actual_multiple_and_opposite_direction(self):
        cfg = self.config('overall')
        book = RoundsBook(cfg=cfg)
        long, short = keys('both'), keys_of('BTC-USDT', -1, 'trend', 'base', 3, 'pos', 'both')
        self.trade(book, long, 'one', -1)
        self.assertEqual(book.ask(short)[0], 1)
        self.trade(book, long, 'two', -1)
        self.assertEqual(book.ask(long)[0], 2.5)
        # The shared cap or lot rounding changes the aggregate result. The
        # count-one deficit is -3 and its own proposed x2 makes back 2.6.
        book.opened('three', long, 4)
        book.closed('three', 1.3)
        first = book.groups[book.gid('overall', long['overall'], 1)]
        self.assertEqual(first['level'], 1)
        self.assertAlmostEqual(first['net'], -.4)


if __name__ == '__main__':
    unittest.main()
