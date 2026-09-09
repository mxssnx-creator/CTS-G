"""Independent filter cells, accounting sources and bounded overview payloads."""
import copy
import json
import pathlib
import sys
import unittest
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from set_engine import SetBook, SetState, slim_hist_row
from set_overview import build_overview, merge_overviews, PREVIEW_PER_GROUP


def state(index=0, **values):
    base = dict(id=f"general:1m:sl0.6:st3:{index}", pack="general", tf="1m",
                sl_ratio=.6, trail_key="", trail_arm=0., trail_give=0., step=3,
                tp_pct=.003, idx=index)
    return SetState(**{**base, **values})


def tape(gross=.003, **values):
    return [dict(t=1_800_000_000 + i * 60, symbol="X-USDT", side="LONG", pnl_pct=gross,
                 pnl=gross - .001, hold_s=60, reason="tp", **values) for i in range(15)]


def book(states):
    out = SetBook()
    out.cost_pct = .10
    out.by_idx = states
    out.sets = {st.id: st for st in states}
    return out


class SetOverviewTests(unittest.TestCase):
    def test_each_range_and_family_survives_the_global_preview_cap(self):
        states = [state(i) for i in range(500)]
        states += [state(500, tp_pct=.012), state(501, kind="trail", trail_key="0.3:0.1")]
        result = build_overview(book(states))
        self.assertEqual(sum(group["setCount"] for group in result["groups"]), 502)
        self.assertEqual({row["tpRange"] for row in result["rows"]}, {"0.3000", "1.2000"})
        self.assertIn("trailing", {row["strategyType"] for row in result["rows"]})
        self.assertLessEqual(len(result["rows"]), len(result["groups"]) * PREVIEW_PER_GROUP)
        json.dumps(result, allow_nan=False)

    def test_system_and_exchange_have_their_own_pf_and_sample_counts(self):
        st = state(hist=tape(.004), live=tape(-.002, exchange_confirmed=True, strategy="core"))
        st.live += tape(.04, exchange_confirmed=False)
        st.live += tape(.04, exchange_confirmed=True, partial=True)
        result = build_overview(book([st]))
        rows = {row["scope"]: row for row in result["rows"]}
        self.assertEqual(rows["system"]["n"], 15)
        self.assertEqual(rows["exchange"]["n"], 15)
        self.assertGreater(rows["system"]["last15Ratio"], 1.1)
        self.assertLess(rows["exchange"]["last15Ratio"], 1.)

    def test_indications_strategies_and_execution_lanes_do_not_mix(self):
        st = state(pack="indications")
        for kind, strat, gross in [("trend", "core", .004), ("break", "core", -.002), ("trend", "block", .002), ("trend", "dca", -.003)]:
            st.live += tape(gross, exchange_confirmed=True, strategy=strat, ind_kind=kind, execution_lane=f"{kind}:{strat}")
        result = build_overview(book([st]))
        rows = {(row["indicationKind"], row["strategyType"]): row for row in result["rows"] if row["scope"] == "exchange"}
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["n"] == 15 for row in rows.values()))
        self.assertGreater(rows[("trend", "normal")]["last15Ratio"], rows[("break", "normal")]["last15Ratio"])
        self.assertEqual(len({row["id"] for row in rows.values()}), 4)

    def test_sibling_direction_windows_stay_independent(self):
        st = state()
        st.live = tape(.004, exchange_confirmed=True, execution_lane="one")
        negative = tape(-.002, exchange_confirmed=True, execution_lane="two")
        for row in negative:
            row["side"] = "SHORT"
        st.live += negative
        rows = [row for row in build_overview(book([st]))["rows"] if row["scope"] == "exchange"]
        self.assertEqual({row["side"] for row in rows}, {"LONG", "SHORT"})
        self.assertEqual(len({row["last15Ratio"] for row in rows}), 2)

    def test_compaction_preserves_additional_strategy_range_lineage(self):
        original = {**tape()[0], "set_id": "general:1m:sl0.6:st3", "pack": "general",
                    "strategy": "block", "tp_pct": .003, "sl_ratio": .6, "step": 3}
        compact = slim_hist_row(original)
        for key in ("set_id", "pack", "strategy", "tp_pct", "sl_ratio", "step"):
            self.assertEqual(compact[key], original[key])
        self.assertNotIn("pnl", compact)
        b = book([])
        b.strategy_hist["block"] = [compact]
        row = build_overview(b)["rows"][0]
        self.assertEqual((row["indicationKind"], row["strategyType"], row["tpRange"]), ("general", "block", "0.3000"))

    def test_legacy_unassigned_ranges_do_not_inherit_new_settings(self):
        b = book([])
        b.min_step_cfg = 30
        b.ind_hist["trend"] = tape(ind_kind="trend")
        b.strategy_hist["dca"] = tape(strategy="dca")
        result = build_overview(b)
        self.assertTrue(all(row["tpRange"] == "unknown" and row["tpPct"] is None for row in result["rows"]))

    def test_disabled_axis_excludes_history_and_exchange_from_overview(self):
        st = state(live=tape(exchange_confirmed=True, strategy="core", axis_key="last:5"))
        axis = [{"parentSetId": st.id, "axisKey": "last:5", "pf": 1.2, "closedN": 5, "qualified": True}]
        result = build_overview(book([st]), axis, axis_enabled=False)
        self.assertNotIn("axis", {row["strategyType"] for row in result["rows"]})
        self.assertNotIn("axis", {group["strategyType"] for group in result["groups"]})

    def test_axis_pf_uses_its_window_without_fabricating_other_metrics(self):
        st = state(hist=tape(.03))
        axis = [{"parentSetId": st.id, "axisKey": "prev:5", "pf": .92, "closedN": 5, "qualified": False}]
        row = next(row for row in build_overview(book([st]), axis)["rows"] if row["strategyType"] == "axis")
        self.assertEqual((row["last15Ratio"], row["n"]), (.92, 5))
        self.assertIsNone(row["maxDdS"])
        self.assertFalse(row["active"])

    def test_overall_keeps_desk_results_separate_and_adds_counts(self):
        first = build_overview(book([state(hist=tape(.004))]))
        second = build_overview(book([state(hist=tape(-.002))]))
        before = copy.deepcopy(first)
        result = merge_overviews([("Live", first), ("VST demo", second)])
        self.assertEqual(result["groups"][0]["setCount"], 2)
        self.assertEqual(len({row["id"] for row in result["rows"]}), 2)
        self.assertEqual(len({row["last15Ratio"] for row in result["rows"]}), 2)
        self.assertEqual(first, before)


if __name__ == "__main__":
    unittest.main()
