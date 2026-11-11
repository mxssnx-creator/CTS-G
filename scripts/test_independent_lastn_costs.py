"""Offline checks for the compact Last-N archive recount."""
import base64
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_independent_lastn_costs as audit


class IndependentLastNCostTests(unittest.TestCase):
    def test_figures_include_losses_and_separate_no_loss_from_empty(self):
        result = audit.figures([.01, -.02, .03, 0])
        self.assertEqual(result["n"], 4)
        self.assertAlmostEqual(result["pf"], 2.0)
        self.assertIsNone(audit.figures([])["pf"])
        self.assertFalse(audit.figures([])["noLoss"])
        self.assertTrue(audit.figures([.01])["noLoss"])

    def test_recount_rejects_tampered_recorded_totals(self):
        expected = audit.figures([.01, -.02])
        audit.compare_archived(expected, expected, "test")
        with self.assertRaises(ValueError):
            audit.compare_archived(expected, {**expected, "gl": 0}, "test")

    def test_recorded_net_is_not_charged_a_second_time(self):
        audit.compare_engine([.001, -.0005, 0], "test")

    def test_only_known_archive_schema_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.html"
            payload = {"data": {"schema": "cts-independent-lastn-diagnostic-v1"}}
            encoded = base64.b64encode(gzip.compress(json.dumps(payload).encode()))
            path.write_bytes(b'<script id="payload" type="application/octet-stream">' + encoded + b'</script>')
            self.assertEqual(audit.load_archive(path)[1], payload)
            payload["data"]["schema"] = "other"
            encoded = base64.b64encode(gzip.compress(json.dumps(payload).encode()))
            path.write_bytes(b'<script id="payload">' + encoded + b'</script>')
            with self.assertRaises(ValueError):
                audit.load_archive(path)


if __name__ == "__main__":
    unittest.main()
