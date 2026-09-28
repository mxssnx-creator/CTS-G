"""Offline regressions for the desk Stats/Overview payloads (Overall, connections, report)."""
import pathlib
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_http as http
from combo_eval import evaluate_fills
from stats_report import build, render_html

NOW = 1_790_000_000.0


def position(conn, symbol, side, oid, qty=1.0):
    return {"symbol": symbol, "side": side, "qty": qty, "entry": 100.0, "exchangeQty": qty, "ours": True,
            "slOid": f"sl-{oid}", "tpOid": f"tp-{oid}", "connection": conn,
            "trackingScope": f"cts-g:{conn}", "systemId": "cts-g"}


def close(conn, i, pnl, kind="", symbol="BTC-USDT"):
    return {"t": NOW - i * 60, "symbol": symbol, "side": "LONG", "qty": 1, "entry": 100, "exit": 101,
            "pnl": pnl, "pnl_pct": pnl / 100.0, "reason": "tp", "hold_s": 30, "ind_kind": kind,
            "connection": conn, "trackingScope": f"cts-g:{conn}", "systemId": "cts-g"}


def lane_state(conn, opens, **extra):
    groups = len({(p["symbol"], p["side"]) for p in opens})
    orders = len({p["slOid"] for p in opens} | {p["tpOid"] for p in opens})
    state = {
        "running": True, "halted": False, "connection": conn, "systemId": "cts-g", "trackingScope": f"cts-g:{conn}",
        "open": opens, "openCount": len(opens), "logicalPositionCount": len(opens),
        "realPositionCount": groups, "realPositionGroupCount": groups, "realOrderCount": orders,
        "livePositionCount": groups, "liveOrderCount": orders, "exchangeOpenCount": groups,
        "closed": [], "wins": 0, "losses": 0, "pfCost": {"n": 15, "costPct": 0.1, "minPf": 1.15},
    }
    state.update(extra)
    return state


def run_overall(states):
    with patch.object(http, "load_stats", side_effect=lambda cid: states.get(cid, {})), \
         patch.object(http, "unit_state", return_value="active"), \
         patch.object(http, "stats_age", return_value=1), \
         patch.object(http.os.path, "exists", return_value=False):
        return http.merge_overall(), http.connections_blob()


class StatsOverviewTests(unittest.TestCase):
    def test_real_positions_are_counted_per_connection(self):
        # Live: two config lanes share one BTC LONG parent and one SL+TP pair
        # (orders == lanes). VST: three single-lane groups (groups == lanes).
        live = lane_state("bingx-x01", [position("bingx-x01", "BTC-USDT", "LONG", 1),
                                        position("bingx-x01", "BTC-USDT", "LONG", 1)], liveOrderCount=-1)
        vst = lane_state("bingx-x02", [position("bingx-x02", "BTC-USDT", "LONG", 3),
                                       position("bingx-x02", "ETH-USDT", "SHORT", 4),
                                       position("bingx-x02", "SOL-USDT", "LONG", 5)])
        overall, catalog = run_overall({"bingx-x01": live, "bingx-x02": vst})
        types = {row["type"]: row for row in catalog["types"]}
        self.assertEqual(types["live"]["realPositionCount"], 1)
        self.assertEqual(types["live"]["realOrderCount"], 2)
        self.assertEqual(types["vst"]["realPositionCount"], 3)
        self.assertEqual(types["vst"]["realPositionGroupCount"], 3)
        self.assertEqual(types["overall"]["realPositionCount"], 4)
        self.assertEqual(types["overall"]["realOrderCount"], 8)
        # BTC LONG on Live and on VST are two positions, not one merged group.
        self.assertEqual(overall["realPositionCount"], 4)
        self.assertEqual(overall["realPositionGroupCount"], 4)
        merged = {"open": live["open"] + vst["open"]}
        self.assertEqual(http._symbol_direction_count_from_snapshot(merged), 4)
        self.assertEqual(http._symbol_direction_count_from_snapshot(merged, exchange_only=True), 4)

    def test_overall_valid_and_active_share_the_summed_catalog(self):
        def sets(validated, active):
            return {"setCount": 31200, "catalogSetCount": 31200, "internSetCount": 0,
                    "validatedCount": validated, "activeCount": active,
                    "overview": {"version": 1, "generatedAt": NOW, "groups": [], "rows": []}}
        overall, _ = run_overall({
            "bingx-x01": lane_state("bingx-x01", [], sets=sets(20000, 500)),
            "bingx-x02": lane_state("bingx-x02", [], sets=sets(25000, 700)),
        })
        self.assertEqual(overall["sets"]["validatedCount"], 45000)
        self.assertEqual(overall["sets"]["activeCount"], 1200)
        self.assertEqual(overall["sets"]["catalogSetCount"], 62400)
        self.assertLessEqual(overall["sets"]["validatedCount"], overall["sets"]["catalogSetCount"])

    def test_overall_carries_foreign_diagnostics_and_real_pnl_pct(self):
        live = lane_state("bingx-x01", [], systemPnl=2.0, systemRealized=1.5, systemUnrealized=0.5,
                          realizedPnl=1.5, walletUnrealized=0.7, pnlPct=2.0, tradedNotional=100.0,
                          foreignPositionCount=2, foreignOpenOrderCount=1, foreignExposure=40.0,
                          foreignUnrealized=-0.25, foreignRealized=0.75)
        vst = lane_state("bingx-x02", [], systemPnl=-1.0, pnlPct=-1.0, tradedNotional=100.0,
                         foreignPositionCount=1, foreignExposure=10.0, foreignUnrealized=0.05)
        overall, _ = run_overall({"bingx-x01": live, "bingx-x02": vst})
        self.assertEqual(overall["foreignPositionCount"], 3)
        self.assertEqual(overall["foreignOpenOrderCount"], 1)
        self.assertAlmostEqual(overall["foreignExposure"], 50.0)
        self.assertAlmostEqual(overall["foreignUnrealized"], -0.2)
        self.assertAlmostEqual(overall["foreignRealized"], 0.75)
        self.assertAlmostEqual(overall["pnlPct"], 0.5)
        lane = next(row for row in overall["lanes"] if row["type"] == "live")
        for key, value in {"realizedPnl": 1.5, "systemRealized": 1.5, "systemUnrealized": 0.5,
                           "walletUnrealized": 0.7, "foreignRealized": 0.75, "pnlPct": 2.0}.items():
            self.assertAlmostEqual(lane[key], value, msg=key)
        # Unknown traded notional behind a non-zero PnL is not a 0% result.
        del vst["tradedNotional"]
        overall, _ = run_overall({"bingx-x01": live, "bingx-x02": vst})
        self.assertIsNone(overall["pnlPct"])
        overall, _ = run_overall({})
        self.assertIsNone(overall["pnlPct"])

    def test_overall_windows_and_indications_come_from_the_merged_tape(self):
        live = lane_state("bingx-x01", [], closed=[close("bingx-x01", i, 1.0, "trend") for i in range(6)],
                          sets={"enableNeed": 8, "indGate": {"trend": {"n": 50, "pf": 2.0, "ok": True, "validated": True}},
                                "liveOverview": {"evaluationWindows": {"last5": {"n": 5, "pf": 9.0}}}})
        vst = lane_state("bingx-x02", [], closed=[close("bingx-x02", i + 10, -1.0, "trend") for i in range(6)])
        overall, _ = run_overall({"bingx-x01": live, "bingx-x02": vst})
        windows = overall["pfCost"]["evaluationWindows"]
        self.assertEqual(windows["last10"]["n"], 10)
        self.assertEqual(windows["last15"]["n"], 12)
        self.assertNotEqual(windows["last5"]["pf"], 9.0)
        trend = overall["byIndication"]["trend"]
        self.assertEqual(trend["n"], 12)
        self.assertNotEqual(trend["pf"], 2.0)
        self.assertNotIn("indGate", overall["sets"])

    def test_report_recent_tape_lists_newest_closes_first(self):
        closed = [dict(close("bingx-x02", i, 0.5, symbol=f"S{i:02d}-USDT")) for i in range(80)]
        blob = build({"closed": closed, "sets": {"rows": []}, "open": [], "pfCost": {"n": 15}}, cost_pct=0.1, conn="bingx-x02")
        self.assertEqual(blob["closed"][-1]["symbol"], "S00-USDT")
        tape = render_html(blob).split("Recent closed tape", 1)[1]
        shown = re.findall(r"S(\d\d)-USDT", tape)
        self.assertEqual(shown[:2], ["00", "01"])
        self.assertEqual(len(shown), 40)
        self.assertNotIn("79", shown)

    def test_compact_combo_families_use_effective_cost_and_window(self):
        closed = [close("bingx-x02", i, pnl, "trend") for i, pnl in enumerate([0.12, 0.14, -0.05, 0.13, 0.11, 0.2, -0.3, 0.1])]
        policy = {"costPct": 0.12, "n": 5, "minPf": 1.15}
        compact = http.slim_for_ui({"closed": closed, "pfCost": policy})
        expected = evaluate_fills(closed, min_pf=1.15, cost_pct=0.12, pf_n=5)["pfStats"]["overall"]
        default = evaluate_fills(closed, min_pf=1.15)["pfStats"]["overall"]
        self.assertNotEqual(expected["pf"], default["pf"])
        self.assertEqual(compact["pfStats"]["overall"]["pf"], expected["pf"])
        self.assertEqual(compact["pfStats"]["overall"]["evalN"], 5)


if __name__ == "__main__":
    unittest.main()
