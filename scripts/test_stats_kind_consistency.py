"""Regressions: byIndication / byStrategy count the same closes, Overall reads full tapes."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_http as http
import stats_report as sr
from stats_report import IND_KINDS, by_indication, by_strategy, enrich, merge_kind_stats, _row

NOW = 1_790_000_000.0


def close(i, pnl, *, kind="", reason="tp", pack="indications", side="LONG", symbol="BTC-USDT"):
    return {"t": NOW + i * 60, "symbol": symbol, "side": side, "qty": 1, "entry": 100, "exit": 101,
            "pnl": pnl, "pnl_pct": pnl / 100.0, "reason": reason, "hold_s": 30, "ind_kind": kind, "pack": pack}


class KindStrategyConsistency(unittest.TestCase):
    def test_block_and_dca_closes_stay_out_of_indication_kinds(self):
        rows = [enrich(_row(r), 0.15) for r in (
            close(1, 0.5, kind="active", reason="ind:active:tp"),
            close(2, -0.2, kind="active", reason="ind:active:sl"),
            close(3, 0.3, kind="active", reason="block:active:2", pack="block"),
            close(4, 0.1, kind="state", reason="dca:1", pack="dca"),
            close(5, 0.4, kind="state", reason="ind:state:tp"),
        )]
        kinds = by_indication(rows, 0.15)
        strats = by_strategy(rows, 0.15)
        for k in IND_KINDS:
            self.assertEqual(kinds[k]["n"], strats.get(f"indications:{k}", {}).get("n", 0), k)
        self.assertEqual(kinds["active"]["n"], 2)
        self.assertEqual(strats["block"]["n"], 1)
        self.assertEqual(strats["dca"]["n"], 1)

    def test_unknown_ind_kind_is_not_signals(self):
        self.assertEqual(sr._kind_of({"reason": "ind:mystery:tp"}), "")
        self.assertEqual(sr._kind_of({"reason": "ind:signals:tp"}), "signals")
        kinds = by_indication([enrich(_row(close(1, 0.2, reason="ind:mystery:tp")), 0.15)], 0.15)
        self.assertEqual(kinds["signals"]["n"], 0)

    def test_replay_gate_supplies_every_field(self):
        rows = [enrich(_row(close(i, 0.3 if i % 2 else -0.1, kind="move", reason="ind:move:x")), 0.15) for i in range(3)]
        gate = {"move": {"n": 40, "pf": 1.4, "bySide": {"LONG": {"n": 40, "pf": 1.4}}, "ok": True, "validated": True}}
        blob = merge_kind_stats(rows, 0.15, gate=gate)["move"]
        self.assertEqual(blob["source"], "replay")
        self.assertEqual((blob["n"], blob["pf"]), (40, 1.4))
        self.assertEqual(blob["bySide"], gate["move"]["bySide"])
        for key in ("gp", "gl", "net", "wr"):
            self.assertIsNone(blob[key], key)
        # Live tape wins → live values untouched.
        live = merge_kind_stats(rows, 0.15, gate={"move": {"n": 1, "pf": 9.0}})["move"]
        self.assertEqual(live["n"], 3)
        self.assertNotEqual(live["pf"], 9.0)

    def test_ddt_uses_given_cost(self):
        seen = []
        real = sr.drawdown_time_by_symbol

        def spy(rows, *a, **kw):
            seen.append(kw.get("cost_pct"))
            return real(rows, *a, **kw)

        rows = [enrich(_row(close(i, 0.1, kind="state", reason="ind:state:x")), 0.4) for i in range(2)]
        with patch.object(sr, "drawdown_time_by_symbol", side_effect=spy):
            sr.by_symbol(rows, 0.4)
            sr._bucket_stats(rows, 0.4)
            sr.build({"closed": rows, "sets": {"rows": []}, "open": []}, cost_pct=0.4)
        self.assertTrue(seen)
        # Every net DDT call gets the cost; only the explicit gross tape may omit it.
        self.assertLessEqual(seen.count(None), 1)
        self.assertIn(0.4, seen)


class KindFlags(unittest.TestCase):
    def test_every_kind_has_a_settings_flag(self):
        from contracts import INDICATION_KINDS
        from indication_engine import DEFAULT_SETTINGS, KIND_FLAGS, IndicationBook
        self.assertEqual(tuple(KIND_FLAGS), tuple(INDICATION_KINDS))
        for kind, flag in KIND_FLAGS.items():
            self.assertIn(flag, DEFAULT_SETTINGS, kind)
        book = IndicationBook()
        book.settings["typeBreak"] = False
        types = book.snapshot()["types"] if callable(getattr(book, "snapshot", None)) else {}
        if types:
            self.assertEqual(set(types), set(INDICATION_KINDS))
            self.assertFalse(types["break"])

    def test_coverage_indication_types_built_from_kind_flags(self):
        src = (pathlib.Path(__file__).resolve().parents[1] / "server/pulse/pulse_trader.py").read_text()
        self.assertIn('"indicationTypes": {k: bool(self.indications.settings.get(f, True)) for k, f in KIND_FLAGS.items()}', src)


class OverallFullTapes(unittest.TestCase):
    def test_overall_kind_n_sums_per_desk_full_tapes(self):
        states = {}
        for li, lane in enumerate(http.LANES):
            rows = [close(li * 1000 + i, 0.2 if i % 3 else -0.1, kind=IND_KINDS[i % len(IND_KINDS)],
                          reason=f"ind:{IND_KINDS[i % len(IND_KINDS)]}:tp") for i in range(120)]
            states[lane["id"]] = {
                "running": True, "open": [], "closed": rows, "tests": [], "sets": {},
                "coverage": {"indicationTypes": {"state": li == 0, "trend": li == 1}, "indicationHits": {"state": 2}},
            }
        with patch.object(http, "load_stats", side_effect=lambda cid: states[cid]), \
             patch.object(http, "unit_state", return_value="active"), \
             patch.object(http, "stats_age", return_value=1), \
             patch.object(http.os.path, "exists", return_value=False):
            result = http.merge_overall()
        per_desk = [by_indication([enrich(_row(c), 0.15) for c in states[l["id"]]["closed"]], 0.15) for l in http.LANES]
        for k in IND_KINDS:
            self.assertEqual(result["byIndication"][k]["n"], sum(d[k]["n"] for d in per_desk), k)
            self.assertEqual(result["byIndication"][k]["n"], result["byStrategy"].get(f"indications:{k}", {}).get("n"), k)
        self.assertEqual(sum(result["byIndication"][k]["n"] for k in IND_KINDS), 240)
        state = result["byIndication"]["state"]
        self.assertIsNone(state["gp"])
        self.assertEqual(set(state["byUnit"]), {"USDT", "VST"})
        self.assertTrue(state["enabled"])
        self.assertTrue(result["byIndication"]["trend"]["enabled"])
        self.assertEqual(state["hits"], 4)

    def test_merge_indication_types_unions_kinds(self):
        merged = http.merge_indication_types([{"indicationTypes": {"state": False, "move": True}},
                                              {"indicationTypes": {"state": True, "break": False}}])
        self.assertEqual(merged, {"state": True, "move": True, "break": False})


if __name__ == "__main__":
    unittest.main()
