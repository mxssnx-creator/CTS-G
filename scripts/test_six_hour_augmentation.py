"""Supplemental evidence must preserve cash, own scope and causality."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import pickle
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import augment_six_hour_audit as augment


def book():
    return SimpleNamespace(cost_pct=.18, max_dd_s=3600, pf_n=2, block_counts=[], dca_max_steps=0,
        eval_need=lambda: 2, _stage_window_ns=lambda: (2, 2, 2),
        ddt_window=lambda: 10, stage_min_pf={"base":1.1, "main":1.1, "real":1.1})


def raw_result(symbol="TEST-USDT"):
    # An identically named indication's positive long tape cannot qualify
    # either a different own Set or its own negative short direction.
    rows = [(0, 1, 10, 20, .006), (0, 1, 30, 40, .006), (0, 1, 50, 60, .006),
            (0, 1, 370, 400, .006), (0, -1, 10, 20, -.003), (0, -1, 30, 40, -.003),
            (0, -1, 370, 400, -.003), (1, 1, 10, 20, -.003), (1, 1, 30, 40, -.003),
            (1, 1, 370, 400, -.003)]
    result = dict(symbol=symbol, startS=1_800_000_000,
        bars=[[100, 101, 99, 100, 10] for _ in range(720)], funding=[], reasons=["tp"],
        coverage=[dict(lane="signals|direct_tf:1m", requestedConfigs=5, evaluatedConfigs=5)],
        catalog=[dict(localUid=0, symbolScoped=True, lane="signals|direct_tf:1m", set_id="a", set_ids=["a","a2","a3"], mult=3, kind="base"),
                 dict(localUid=1, symbolScoped=True, lane="signals|direct_tf:1m", set_id="b", set_ids=["b","b2"], mult=2, kind="trail")])
    result["core"] = {name: np.asarray([row[i] for row in rows], dtype=float if name=="raw" else int)
                      for i, name in enumerate(("uid", "side", "entry", "exit", "raw"))}
    result["core"]["reason"] = np.zeros(len(rows), dtype=int)
    return result


class SixHourAugmentationTests(unittest.TestCase):
    def summary(self):
        return augment.summarize_symbol(raw_result(), None, book(), dict(requestedDescendantDirectionConfigs=0))

    def test_real_model_signature_handles_indented_book_methods(self):
        signature = augment.calculation_model_signature()
        self.assertEqual(len(signature),64)
        self.assertEqual(signature,augment.calculation_model_signature())

    def test_all_seventeen_kinds_and_five_stages_with_empty_pf_none(self):
        summary = self.summary()
        self.assertEqual(len(summary["preparationHourly"]), 6)
        self.assertEqual(len(summary["byIndicationHourly"]), 6)
        self.assertEqual(len(summary["snapshots"]), 13)
        first = summary["byIndicationHourly"][0]["types"]
        self.assertEqual(set(first), set(augment.audit.INDICATION_KINDS))
        for kind, value in first.items():
            self.assertEqual(set(value["stages"]), set(augment.STAGES))
            if kind != "signals":
                for stats in value["stages"].values():
                    self.assertEqual(stats["n"], 0)
                    self.assertIsNone(stats["pf"])

    def test_alias_weights_are_numerical_observations_not_borrowed_admission(self):
        summary = self.summary()
        stages = summary["byIndicationHourly"][0]["types"]["signals"]["stages"]
        self.assertEqual(stages["preBase"]["n"], 8)
        self.assertEqual(stages["admitted"]["n"], 3)
        self.assertEqual(stages["admitted"]["distinctExitBehaviours"], 1)
        self.assertAlmostEqual(stages["admitted"]["pf"], 1 + .1 * ((.006-.0018)/.0018), places=6)
        endpoint = summary["snapshots"][6]
        self.assertEqual(endpoint["qualifiedDirectionConfigs"]["Base"], 3)
        self.assertEqual(endpoint["qualifiedSets"]["Base"], 3)

    def test_same_minute_or_later_close_never_qualifies_earlier_preparation_entry(self):
        result = raw_result()
        result["core"]["exit"][:2] = [50, 60]
        catalog, tapes, _, evidence = augment.audit.build_causal_candidates([result], book())
        mask = tapes["entry"] == 50
        rows = augment.mark_own_stages({name:value[mask] for name,value in tapes.items()}, evidence, book())
        self.assertEqual(rows["countBeforeEntry"].tolist(), [0])
        self.assertFalse(rows["base_ok"][0])
        self.assertFalse(rows["admitted"][0])

    def test_own_ddt_does_not_cross_configuration_or_direction_boundary(self):
        evidence = augment.audit.account_model.Evidence(np.array([0,0,0,1,1]),
            np.array([10,20,40,100,120]), np.array([-.01,-.01,.04,-.01,.02]), .18,
            tiebreak=np.zeros(5,dtype=int))
        self.assertEqual(augment.own_ddt_prefix(evidence, .18).tolist(), [0,600,1800,0,1200])

    def test_configured_own_ddt_blocks_real_positive_tape_independently(self):
        result = raw_result()
        # Positive mean with a 30-minute own drawdown episode; restrictive
        # DDT blocks its execution but leaves its Real-PF gate visible.
        result["core"]["raw"][:3] = [.02,-.004,.014]
        result["core"]["exit"][:3] = [20,40,70]
        scope_book = book()
        scope_book.max_dd_s = 600
        _, _, candidates, _ = augment.audit.build_causal_candidates([result], scope_book)
        positive = (candidates["uid"]==0) & (candidates["side"]==1)
        self.assertTrue(candidates["real_ok"][positive][0])
        self.assertFalse(candidates["admitted"][positive][0])
        summary = augment.summarize_symbol(result, None, scope_book, dict(requestedDescendantDirectionConfigs=0))
        endpoint = summary["snapshots"][6]
        self.assertEqual(endpoint["ddBlockedDirectionConfigs"], 3)
        self.assertEqual(endpoint["qualifiedDirectionConfigs"]["Real"], 3)
        self.assertEqual(endpoint["qualifiedDirectionConfigs"]["admitted"], 0)

    def test_incomplete_window_is_descriptive_and_not_positive_gate(self):
        result = raw_result()
        result["core"]["exit"][:3] = [20,90,100]
        summary = augment.summarize_symbol(result, None, book(), dict(requestedDescendantDirectionConfigs=0))
        # The flat initial endpoint is unavailable, and a later single own
        # positive close is descriptive but cannot satisfy a two-close gate.
        initial = summary["snapshots"][0]
        self.assertIsNone(initial["ownWindowPf"]["Base"]["pfMean"])
        self.assertEqual(initial["qualifiedDirectionConfigs"]["Base"], 0)
        endpoint = summary["snapshots"][1]
        self.assertIsNotNone(endpoint["ownWindowPf"]["Base"]["pfMean"])
        self.assertEqual(endpoint["qualifiedDirectionConfigs"]["Base"], 0)

    def test_grouped_hourly_stats_match_the_uncompressed_reference(self):
        source = raw_result()
        catalog, _, rows, _ = augment.audit.build_causal_candidates([source], book())
        actual = augment.hourly_categories(catalog, rows, .18, 360, source["startS"], ["all"], lambda _: "all", "types")
        reference = augment.audit.stage_report(catalog, rows, .18, 360, source["startS"])
        for grouped, expected in zip(actual, reference):
            self.assertEqual(grouped["types"]["all"]["stages"], expected["stages"])

    def test_merged_pf_uses_observation_weights_not_mean_of_symbol_pfs(self):
        merged = augment.combine_stats([
            augment.audit.canonical_stats([.006], .18, [3]),
            augment.audit.canonical_stats([-.003], .18, [2])], .18)
        expanded = augment.audit.canonical_stats([.006]*3+[-.003]*2, .18)
        for field in ("n","pf","wins","losses"):
            self.assertEqual(merged[field], expanded[field])
        self.assertAlmostEqual(merged["netSumPctUnits"], expanded["netSumPctUnits"])

    def test_original_account_admission_and_provenance_are_byte_equivalent(self):
        report = dict(costPct=.18, profile="baseline", sourceManifest={"sha":"immutable"},
            account={"totals":{"balanceEnd":123.00000001,"orders":{"entry":7}},"closedPositions":[{"net":.031}]},
            stageHourly=[{"admitted":7}], modelSignature="old-model", scoringSignature="old-score")
        original = copy.deepcopy(report)
        supplemented = augment.supplement_report(report, [self.summary()], [{"raw":"sha"}], "new-model")
        for name, value in original.items():
            self.assertEqual(supplemented[name], value)
        self.assertEqual(report, original)
        self.assertEqual(supplemented["augmentation"]["preservedAccountSHA256"], augment.digest_json(original["account"]))
        self.assertEqual(supplemented["augmentation"]["sourceSymbols"], 1)

    def test_merged_ddt_is_maximum_and_counts_sum_across_independent_scopes(self):
        endpoint = self.summary()["preparationHourly"][0]["endpoint"]
        second = copy.deepcopy(endpoint)
        endpoint["maxOwnObservedDdtS"], second["maxOwnObservedDdtS"] = 600,1200
        merged = augment.combine_endpoints([endpoint,second])
        self.assertEqual(merged["maxOwnObservedDdtS"], 1200)
        self.assertEqual(merged["qualifiedDirectionConfigs"]["Base"], 6)

    def test_calculation_supplement_reuses_evidence_without_overwriting_later_account(self):
        report = dict(costPct=.18, profile="baseline", sourceManifest={"sha":"immutable"},
            account={"totals":{"balanceEnd":1000}}, engine={"baseWindow":2}, scoringSignature="frozen")
        supplemented = augment.supplement_report(report, [self.summary()], [], "new-model")
        later = dict(report, account={"totals":{"balanceEnd":1000.012345678,"orders":{"entry":3}}})
        merged = augment.merge_calculation_supplement(later, augment.calculation_supplement(supplemented))
        self.assertEqual(merged["account"], later["account"])
        self.assertEqual(merged["augmentation"]["calculationSourceAccountSHA256"], augment.digest_json(report["account"]))
        self.assertEqual(merged["augmentation"]["preservedAccountSHA256"], augment.digest_json(later["account"]))
        with self.assertRaisesRegex(ValueError, "source provenance"):
            augment.merge_calculation_supplement(dict(later, sourceManifest={"sha":"different"}), augment.calculation_supplement(supplemented))
        with self.assertRaisesRegex(ValueError, "scoring signature"):
            augment.merge_calculation_supplement(dict(later, scoringSignature="different"), augment.calculation_supplement(supplemented))

    def test_supplement_refuses_changed_original_execution_admission(self):
        result = raw_result()
        catalog, _, candidates, _ = augment.audit.build_causal_candidates([result], book())
        original = dict(costPct=.18, stageHourly=augment.audit.stage_report(catalog, candidates, .18, 360, result["startS"]))
        strategy = self.summary()["byStrategyHourly"]
        self.assertTrue(augment.verify_original_execution(original, strategy))
        original["stageHourly"][0]["stages"]["admitted"]["n"] += 1
        with self.assertRaisesRegex(ValueError, "execution cohort differs"):
            augment.verify_original_execution(original, strategy)

    def test_stale_empty_derived_cache_requires_all_endpoint_zero_parent_proof(self):
        from axis_grid import axis_ranges
        result = raw_result()
        result["core"]["raw"][:] = -.003
        scope_book = book()
        requested = 5 * 2 * len(axis_ranges(scope_book))
        summary = augment.summarize_symbol(result, None, scope_book,
            dict(ownBaseEligibleParents=0, requestedDescendantDirectionConfigs=requested),
            require_zero_parent_proof=True)
        self.assertEqual(summary["zeroParentProof"]["qualifiedParents"], 0)
        self.assertEqual(summary["zeroParentProof"]["scannedOwnClosedEndpoints"], 10)
        # Final loss must not conceal an earlier positive qualifying window.
        result["core"]["raw"][:2] = .006
        result["core"]["raw"][2:4] = -.03
        with self.assertRaisesRegex(ValueError, "eligible descendant parent"):
            augment.summarize_symbol(result, None, scope_book,
                dict(ownBaseEligibleParents=0, requestedDescendantDirectionConfigs=requested),
                require_zero_parent_proof=True)

    def test_durable_symbol_checkpoint_skips_recalculation_only_when_inputs_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, cache, checkpoints = root/"data", root/"input-cache", root/"completed"
            data.mkdir()
            cache.mkdir()
            (data/"funding").mkdir()
            symbol = "TEST-USDT"
            result = raw_result(symbol)
            augment.audit.atomic_json(data/(symbol+".json"), {"symbol":symbol,"rows":[[(result["startS"]+i*60)*1000,bar] for i,bar in enumerate(result["bars"])]})
            augment.audit.atomic_json(data/"overlay.json", {})
            augment.audit.atomic_json(data/"funding"/(symbol+".json"), [])
            result["core"]["raw"][:] = -.003
            source = dict(symbol=symbol, sha256=augment.sha_file(data/(symbol+".json")),
                fundingSha256=augment.sha_file(data/"funding"/(symbol+".json")))
            overlay_sha = augment.sha_file(data/"overlay.json")
            result["signature"] = ("frozen-price-physics", source["sha256"], overlay_sha)
            raw_path = cache/(symbol+".pkl")
            raw_path.write_bytes(pickle.dumps(result))
            derived = dict(signature=augment.derived_signature(result, {}, "baseline", data), result=None,
                coverage=dict(complete=True,ownBaseEligibleParents=0,requestedDescendantDirectionConfigs=0))
            (cache/(symbol+".baseline.derived.pkl")).write_bytes(pickle.dumps(derived))
            report = dict(profile="baseline",costPct=.18,symbols=[symbol],
                sourceManifest=dict(symbols=[source],overlaySha256=overlay_sha,historyStart=result["startS"]*1000,
                                    end=result["startS"]*1000+720*60000),
                engine=dict(baseWindow=2,mainWindow=2,realWindow=2,requiredSamples=2),
                account={"totals":{"balanceEnd":1000.00000001}})
            report_path = root/"original.json"
            augment.audit.atomic_json(report_path, report)
            scope_book = book()
            scope_book.load = lambda _:None
            with patch.object(augment.audit.se,"SetBook",return_value=scope_book), \
                 patch.object(augment,"calculation_model_signature",return_value="frozen-math"), \
                 patch.object(augment,"write_outputs"):
                first = augment.augment_report(report_path,cache,data,checkpoint_dir=checkpoints,max_anon_before_builder_gib=0)
                self.assertTrue((checkpoints/(symbol+".baseline.json")).exists())
                with patch.object(augment,"summarize_symbol",side_effect=AssertionError("must use completed checkpoint")):
                    second = augment.augment_report(report_path,cache,data,checkpoint_dir=checkpoints,max_anon_before_builder_gib=0)
                    self.assertEqual(first["preparationHourly"],second["preparationHourly"])
                    self.assertEqual(json.loads(report_path.read_text()),report)
                    result["core"]["raw"][0] += .001
                    raw_path.write_bytes(pickle.dumps(result))
                    with self.assertRaisesRegex(AssertionError,"completed checkpoint"):
                        augment.augment_report(report_path,cache,data,checkpoint_dir=checkpoints,max_anon_before_builder_gib=0)

    def test_forty_eight_hour_span_has_only_forty_seven_complete_utc_buckets(self):
        rows = [[(8+i)*60000,[100,101,99,100,1]] for i in range(2880)]
        evidence = augment.closed_utc_hour_evidence(rows)
        self.assertEqual(evidence["dataSpanHours"],48)
        self.assertEqual(evidence["usableClosedHourlyBars"],47)
        missing = [row for row in rows if row[0] != 60*60000]
        self.assertEqual(augment.closed_utc_hour_evidence(missing)["usableClosedHourlyBars"],46)
        self.assertEqual(augment.closed_utc_hour_evidence(rows+rows[60:61])["usableClosedHourlyBars"],46)


if __name__ == "__main__":
    unittest.main()
