"""HTTP/UI payload regressions for large independent Set catalogs."""
import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
from pulse_http import MAX_JSON_RESPONSE_BYTES, env_listen_port, slim_for_ui


class HttpPayloadTests(unittest.TestCase):
    def test_nested_set_coverage_is_compacted_without_losing_counts(self):
        blob = {
            "sets": {
                "coverage": {
                    "qualifiedParentIds": {
                        "base": [f"b-{i}" for i in range(50000)],
                        "main": [f"m-{i}" for i in range(50000)],
                        "real": [f"r-{i}" for i in range(50000)],
                    },
                    "processingSetIds": [f"p-{i}" for i in range(1000)],
                }
            }
        }
        compact = slim_for_ui(blob)
        coverage = compact["sets"]["coverage"]
        self.assertEqual(coverage["qualifiedParentIdCounts"], {"base": 50000, "main": 50000, "real": 50000})
        self.assertEqual({stage: len(ids) for stage, ids in coverage["qualifiedParentIds"].items()},
                         {"base": 64, "main": 64, "real": 64})
        self.assertEqual(coverage["processingSetIdCount"], 1000)
        self.assertEqual(len(coverage["processingSetIds"]), 256)
        self.assertLess(len(json.dumps(compact, separators=(",", ":"))), MAX_JSON_RESPONSE_BYTES)

    def test_env_listen_port_coerces_empty_and_invalid(self):
        os.environ["PULSE_PORT"] = ""
        self.assertEqual(env_listen_port(), 3015)
        os.environ["PULSE_PORT"] = "not-a-port"
        self.assertEqual(env_listen_port(), 3015)
        os.environ["PULSE_PORT"] = "80"
        self.assertEqual(env_listen_port(), 3015)
        os.environ["PULSE_PORT"] = "3016"
        self.assertEqual(env_listen_port(), 3016)
        os.environ.pop("PULSE_PORT", None)
        self.assertEqual(env_listen_port(), 3015)

    def test_hist_test_payload_is_capped(self):
        blob = {
            "histTest": {
                "enabled": True,
                "phase": "evaluate",
                "internSymbols": [f"S{i}-USDT" for i in range(200)],
                "symbols": [f"T{i}-USDT" for i in range(200)],
                "runningSets": [f"set-{i}" for i in range(80)],
            }
        }
        compact = slim_for_ui(blob)
        ht = compact["histTest"]
        self.assertEqual(len(ht["internSymbols"]), 50)
        self.assertEqual(len(ht["symbols"]), 50)
        self.assertEqual(len(ht["runningSets"]), 24)
        self.assertTrue(ht["enabled"])

    def test_hist_test_coordinations_override_live_combo(self):
        blob = {
            "closed": [{"pnl": 1, "t": 1}],
            "pfStats": {"overall": {"pf": 0.9, "n": 1}},
            "histTest": {
                "enabled": True,
                "ownsCatalog": True,
                "phase": "ready",
                "pfStats": {"overall": {"pf": 1.4, "n": 40}},
                "withWithout": {"block": {"with": {"pf": 1.5, "n": 10}}},
                "comboMatrix": [{"indication": "combined", "strategy": "block", "validated": True, "pf": 1.5, "n": 10}],
                "successfulConfigs": [{"setId": "indications:1m:sl0.6:st8", "validated": True}],
                "selectedCoordinations": [{"id": "indications:1m:sl0.6:st8", "strategy": "block"}],
            },
        }
        compact = slim_for_ui(blob)
        self.assertEqual(compact["pfStats"]["overall"]["pf"], 1.4)
        self.assertIn("block", compact["withWithout"])
        self.assertEqual(compact["comboMatrix"][0]["strategy"], "block")
        self.assertEqual(compact["selectedCoordinations"][0]["strategy"], "block")


if __name__ == "__main__":
    unittest.main()
