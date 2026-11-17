"""Regression tests for Set Block relation and per-count deficit independence."""
import ast
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "pulse"))
import block_rules as br
from set_block import SetBlockBook

BLOCK = types.SimpleNamespace(max_stack=2, volume_ratio=.5, min_samples=1, pf_ratio=.8, default_min_pf=1.1)
SET_A, SET_B = "general:L:1m:sl1.00:st20", "general:L:1m:sl2.00:st20"


def parent(set_id, name):
    return types.SimpleNamespace(set_id=set_id, lane="pos", symbol="A-USDT", side="LONG", client_id=name,
        opened_at=1000., base_entry=100., base_qty=1., qty=1., entry=100., tp=102., block_fills=[],
        exit_fee_qty=0., exit_fee_usdt=0., base_entry_fee_usdt=None)


class SetBlockScopeTests(unittest.TestCase):
    def cfg(self):
        return br.clean_cfg({"blockCounts": [{"step": 1, "ratio": .5}, {"step": 1, "ratio": .5}],
            "blockSkipFirst": 0, "blockMinObservations": 1, "blockRelations": ["step"], "blockGuard": True})

    def test_all_relation_types_preserve_set_identity(self):
        cfg = br.clean_cfg({"blockRelations": list(br.RELATIONS)})
        a = br.relation_keys(cfg, SET_A, "pos", "LONG", "A-USDT", "general", 20)
        b = br.relation_keys(cfg, SET_B, "pos", "LONG", "A-USDT", "general", 20)
        self.assertTrue(set(a).isdisjoint(b))
        same_set_new_symbol = br.relation_keys(cfg, SET_A, "pos", "LONG", "B-USDT", "general", 20)
        self.assertNotEqual(a[0], same_set_new_symbol[0])  # symbol relation
        self.assertEqual(a[1:], same_set_new_symbol[1:])  # other relations remain within Set

    def test_losing_count_does_not_hold_another_count_or_set_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "book.json")
            book = SetBlockBook(path); book.cfg = self.cfg()
            for i, sid in enumerate((SET_A, SET_B)):
                p = parent(sid, "teach" + str(i))
                book.observe(p, 100.25, 2000. + i * 100, BLOCK, .1, 3600.)
                book.close(p, 102., 2050. + i * 100, BLOCK, .1)
            losing = parent(SET_A, "losing")
            add = book.candidate(losing, 100.12, 3000., BLOCK, .1, 3600., floor=1.1)
            self.assertEqual(add["group"], 1)
            losing.block_fills.append({"group": 1, "qty": add["qty"], "price": 100.12})
            losing.qty += add["qty"]
            book.close(losing, 99.8, 3100., BLOCK, .1)
            book = SetBlockBook(path); book.cfg = self.cfg()
            next_same = book.candidate(parent(SET_A, "same"), 100.25, 3200., BLOCK, .1, 3600., floor=1.1)
            next_other = book.candidate(parent(SET_B, "other"), 100.12, 3300., BLOCK, .1, 3600., floor=1.1)
            self.assertEqual(next_same and next_same["group"], 2)
            self.assertEqual(next_other and next_other["group"], 1)
            guard = book.snapshot(3300., BLOCK, .1, 3600.)["guard"]
            self.assertNotIn("net", guard)  # overlapping relation totals must not be added together
            self.assertTrue(any(row["set"] == SET_A and row["count"] == 1 and row["holding"] for row in guard["byScope"]))
            # The held count must be able to gather paper evidence despite its
            # losing executed record, recover its own deficit and resume.
            shadow = parent(SET_A, "shadow")
            self.assertIsNone(book.candidate(shadow, 100.12, 3400., BLOCK, .1, 3600., floor=1.1))
            book.close(shadow, 102., 3500., BLOCK, .1)
            book = SetBlockBook(path); book.cfg = self.cfg()
            resumed = book.candidate(parent(SET_A, "resumed"), 100.12, 3600., BLOCK, .1, 3600., floor=1.1)
            self.assertEqual(resumed and resumed["group"], 1)
            ledger = book.ledger(book.rel_keys(book.cfg, shadow)[0])
            self.assertEqual(ledger["executed"]["1"][0], 1)  # actual loss stays in statistics
            self.assertEqual(ledger["qualification"]["1"][0], 0)

    def test_legacy_pooled_deficit_is_retained_as_a_hold(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "book.json")
            with open(path, "w") as f:
                json.dump({"version": 2, "parents": {}, "ledgers": {}, "guard": {"net": -.2, "shadow": .05}}, f)
            book = SetBlockBook(path); book.cfg = self.cfg()
            self.assertTrue(book.snapshot(2000., BLOCK, .1, 3600.)["guard"]["legacyBlocked"])
            book.save()
            reloaded = SetBlockBook(path); reloaded.cfg = self.cfg()
            self.assertEqual(reloaded.legacy_guard, {"net": -.2, "shadow": .05})
            self.assertTrue(reloaded.snapshot(2000., BLOCK, .1, 3600.)["guard"]["legacyBlocked"])

    def test_policy_edit_and_restart_close_against_the_captured_steps(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "book.json")
            book = SetBlockBook(path)
            book.cfg = self.cfg()
            old = book.cfg
            p = parent(SET_A, "changed-while-open")
            book.observe(p, 100.12, 1000., BLOCK, .1, 3600.)
            old_key = book.rel_keys(old, p)[0]
            new = br.clean_cfg({**old.settings(), "blockCounts": [{"step": 3, "ratio": .5}]})
            fresh = SetBlockBook(path)
            fresh.cfg = new
            new_key = fresh.rel_keys(new, p)[0]
            self.assertNotEqual(old_key, new_key)
            self.assertIsNone(fresh.candidate(p, 100.35, 1200., BLOCK, .2, 3600.))
            fresh.close(p, 101., 1300., BLOCK, .2)
            self.assertIn(old_key, fresh.ledgers)
            self.assertNotIn(new_key, fresh.ledgers)
            self.assertEqual(set(fresh.ledgers[old_key]["paper"]), {"1"})
            self.assertAlmostEqual(fresh.ledgers[old_key]["paper"]["1"][1], 101. / 100.12 - 1 - .001)
            self.assertEqual(fresh.snapshot(1500., BLOCK, .2, 3600.)["archivedLedgers"], 1)

    def test_carry_and_pause_are_individual_when_only_count_two_fills(self):
        with tempfile.TemporaryDirectory() as d:
            book = SetBlockBook(os.path.join(d, "book.json"))
            book.cfg = br.clean_cfg({**self.cfg().settings(), "blockCarry": True, "blockPauseSteps": 2, "blockGuard": False})
            p = parent(SET_A, "only-count-two")
            book.observe(p, 100.25, 1000., BLOCK, .1, 3600.)
            p.block_fills.append({"group": 2, "qty": .5, "price": 100.25})
            p.qty += .5
            book.close(p, 99.8, 1100., BLOCK, .1)
            relation = book.ledgers[book.rel_keys(book.cfg, p)[0]]
            self.assertEqual(br.count_life(relation, "1")["level"], 0)
            self.assertEqual(br.count_life(relation, "2")["level"], 1)
            self.assertEqual(book.entry_scale(SET_A, "pos", "LONG", "A-USDT", BLOCK), 1.5)
            q = parent(SET_A, "recovery")
            book.observe(q, 100.25, 1200., BLOCK, .1, 3600.)
            book.close(q, 103., 1300., BLOCK, .1)
            self.assertEqual(br.count_life(relation, "1")["pause"], 0)
            self.assertEqual(br.count_life(relation, "2")["pause"], 2)

    def test_auto_rechecks_losing_count_with_guard_off_and_preserves_actual_history(self):
        with tempfile.TemporaryDirectory() as d:
            book = SetBlockBook(os.path.join(d, "book.json"))
            book.cfg = br.clean_cfg({**self.cfg().settings(), "blockAddLastN": 3, "blockAddAutoMin": 3, "blockGuard": False})
            p = parent(SET_A, "before")
            ledger = book.ledger(book.rel_keys(book.cfg, p)[0], create=True)
            for _ in range(3):
                br.add_observation(book.cfg, ledger, "1", .015, "paper")
                br.add_observation(book.cfg, ledger, "1", -.05, "executed")
            for i in range(3):
                p = parent(SET_A, f"shadow-{i}")
                self.assertIsNone(book.candidate(p, 100.12, 1500. + 100 * i, BLOCK, .1, 3600., floor=1.1))
                self.assertIn("1", book.parents[book.key(p)]["shadow"])
                book.close(p, 102., 1550. + 100 * i, BLOCK, .1)
            self.assertEqual(ledger["executed"]["1"][0], 3)
            self.assertEqual(ledger["executed"]["1"][1], 0)
            r = book.candidate(parent(SET_A, "qualified-again"), 100.12, 2000., BLOCK, .1, 3600., floor=1.1)
            self.assertEqual(r["group"], 1)
            self.assertIsNone(book.candidate(parent(SET_B, "sibling"), 100.12, 2000., BLOCK, .1, 3600., floor=1.1))

    def test_auto_keeps_an_unknown_close_in_the_exact_count_window(self):
        with tempfile.TemporaryDirectory() as d:
            book = SetBlockBook(os.path.join(d, "book.json"))
            book.cfg = br.clean_cfg({**self.cfg().settings(), "blockAddLastN": 4, "blockAddAutoMin": 3})
            p = parent(SET_A, "unknown")
            rkey = book.rel_keys(book.cfg, p)[0]
            ledger = book.ledger(rkey, create=True)
            for _ in range(3):
                br.add_observation(book.cfg, ledger, "1", .02, "paper")
            book.observe(p, 100.12, 1000., BLOCK, .1, 3600.)
            book.close(p, float("nan"), 1100., BLOCK, .1, score=False)
            self.assertEqual(ledger["paper"]["1"][0], 3)
            self.assertIsNone(ledger["paperRecent"]["1"][-1])
            self.assertIsNone(book.candidate(parent(SET_A, "wait"), 100.12, 1200., BLOCK, .1, 3600.))

    def test_shadow_recovery_still_requires_other_relations(self):
        with tempfile.TemporaryDirectory() as d:
            book = SetBlockBook(os.path.join(d, "book.json")); book.cfg = self.cfg()
            book.cfg.relations = ("set", "symbol")
            p = parent(SET_A, "teach")
            book.observe(p, 100.25, 1000., BLOCK, .1, 3600.)
            book.close(p, 102., 1100., BLOCK, .1)
            set_key, symbol_key = book.rel_keys(book.cfg, p)
            book.guards[br.guard_key(set_key, 1)] = {"net": -.1, "shadow": 0.}
            book.ledgers[symbol_key]["qualification"] = {"1": [1, 0., .1]}
            q = parent(SET_A, "blocked")
            self.assertIsNone(book.candidate(q, 100.1, 1200., BLOCK, .1, 3600., floor=1.1))
            self.assertNotIn("1", book.parents[book.key(q)].get("shadow", {}))

    def test_scoped_guard_replay_matches_engine_decisions_and_cash(self):
        stream = [(SET_A, 2, 102.), (SET_B, 2, 102.), (SET_A, 1, 99.8),
                  (SET_A, 2, 99.9), (SET_B, 1, 102.), (SET_A, 1, 102.), (SET_A, 1, 102.)]
        self.assert_matches_ordered_parents(self.cfg(), stream, [(2, 1), (3, 2), (4, 1), (6, 1)])

    def test_replay_matches_native_last_n_pause_and_requalification(self):
        cfg = br.clean_cfg({**self.cfg().settings(), "blockAddLastN": 3, "blockAddAutoMin": 3, "blockGuard": False})
        stream = [(SET_A, 1, price) for price in [103.] * 3 + [99.6] * 3 + [100.9] * 5]
        self.assert_matches_ordered_parents(cfg, stream, [(3, 1), (4, 1), (5, 1), (10, 1)])

    def test_shadow_cannot_repay_a_relation_that_was_clear_at_decision(self):
        cfg = br.clean_cfg({**self.cfg().settings(), "blockRelations": ["set", "symbol"]})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "book.json")
            book = SetBlockBook(path)
            book.cfg = cfg
            p = parent(SET_A, "teach")
            book.observe(p, 100.12, 1000., BLOCK, .1, 3600.)
            book.close(p, 102., 1100., BLOCK, .1)
            set_key, symbol_key = (br.guard_key(k, 1) for k in book.rel_keys(cfg, p))
            book.guards[set_key] = {"net": -.003, "shadow": 0.}
            p = parent(SET_A, "pending-shadow")
            self.assertIsNone(book.candidate(p, 100.12, 1200., BLOCK, .1, 3600., floor=1.1))
            book.guards[symbol_key] = {"net": -.005, "shadow": 0.}
            book.save()
            book = SetBlockBook(path)
            book.cfg = cfg
            book.close(p, 102., 1300., BLOCK, .1)
            self.assertEqual(book.guards[set_key], br.new_guard())
            self.assertEqual(book.guards[symbol_key], {"net": -.005, "shadow": 0.})

    def assert_matches_ordered_parents(self, cfg, stream, expected):
        """Exercise the native replay function and engine on the same ordered parent stream."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import stack_eval as replay
        tree = ast.parse(Path(replay.__file__).read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        fn = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == "block_sim2")
        cf = .001
        times = [float(i * 60) for i in range(len(stream) * 3)]
        stubs = {sid: types.SimpleNamespace(id=sid, pack="general", step=20, idx=i, kind="exit")
                 for i, sid in enumerate((SET_A, SET_B))}
        def first_hit(_symbol, begin, _end, level, _side):
            index = begin // 3
            count = 1 if level < 100.15 else 2
            return begin if count <= stream[index][1] else -1
        ctx = {"by_id": stubs, "cf": cf, "times": {"A-USDT": times}, "first_hit": first_hit,
               "parent_ok": lambda *a: True, "floor_memo": {},
               "auto": types.SimpleNamespace(apply=lambda *a: None),
               "book": types.SimpleNamespace(win=lambda s: types.SimpleNamespace(real_n=5), floor_for=lambda *a: 1.1)}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), replay.__file__, "exec"), ctx)
        parents = [{"set": sid, "symbol": "A-USDT", "lane": "pos", "t_in": times[3*i], "t": times[3*i+2],
                    "net": (exit_px / 100 - 1) - cf,
                    "tr": {"dir": 1, "entry_px": 100., "exit_px": exit_px, "entry_i": 3*i, "exit_i": 3*i+2}}
                   for i, (sid, _, exit_px) in enumerate(stream)]
        _events, replay_adds, replay_nets = ctx["block_sim2"](parents, cfg, None)
        fired, engine_nets = [], []
        with tempfile.TemporaryDirectory() as d:
            book = SetBlockBook(os.path.join(d, "book.json")); book.cfg = cfg
            for i, (sid, levels, exit_px) in enumerate(stream):
                p = parent(sid, str(i))
                for count in range(1, levels + 1):
                    price = 100. * (1. + count * cf)
                    add = book.candidate(p, price, times[3*i+1], BLOCK, .1, 3600., floor=1.1)
                    if add:
                        fired.append((i, add["group"]))
                        p.block_fills.append({"group": add["group"], "qty": add["qty"], "price": price})
                        p.qty += add["qty"]
                        engine_nets.append(((exit_px / price - 1) - cf) * add["qty"] * price / 100.)
                book.close(p, exit_px, times[3*i+2], BLOCK, .1)
        self.assertEqual(fired, expected)
        self.assertEqual(replay_adds, len(fired))
        self.assertEqual(len(replay_nets), len(engine_nets))
        for actual, expected in zip(replay_nets, engine_nets):
            self.assertAlmostEqual(actual, expected, places=10)


if __name__ == "__main__":
    unittest.main()
