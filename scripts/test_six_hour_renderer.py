"""Report presentation must retain empty evidence and exact execution data."""
import base64
import json
from pathlib import Path
import re
import tempfile
import unittest

from render_six_hour_audit import pf, render_report, render_comparison, download


class RendererTests(unittest.TestCase):
    def report(self):
        return dict(profile="baseline", startEquity=1000, costPct=.18,
                    execution=dict(startUtc="2026-10-14T17:08:00Z", endUtc="2026-10-14T23:08:00Z"),
                    preparation=dict(startUtc="2026-10-14T11:08:00Z", endUtc="2026-10-14T17:08:00Z"),
                    engine=dict(requiredSamples=50, baseWindow=50, mainWindow=30, realWindow=30, minPf=1.1),
                    coverage=dict(kinds=["ddt", "tactic"], lanes=[dict(lane="tactic|normal", symbol="A", requestedConfigs=2, evaluatedConfigs=2, signalBars=0)]),
                    account=dict(totals=dict(equityEnd=1000, pnl=0, closed=dict(n=0, pf=1.5)),
                                 hourly=[dict(hour=1, byStrategy=dict(dca=dict(n=0, pf=1.5, hourContributionUsdt=-.37,
                                           contributionUsdt=-.37, cumulativeDdUsdt=.5, sharedCapitalDdPct=.05,
                                           closedNetUsdtToDate=0, openMarkUsdt=-.2, openEntryFeesUsdt=.2, openFundingUsdt=.03)))],
                                 closedPositions=[], openedPositions=[], openPositions=[]))

    def test_empty_pf_is_unavailable_and_valid_zero_is_preserved(self):
        self.assertEqual(pf(dict(n=0, pf=1.5)), "—")
        self.assertEqual(pf(dict(n=1, pf=0)), "0.0000")
        self.assertEqual(pf(dict(n=1, pf=None)), "—")

    def test_actual_contributions_and_no_evidence_are_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "report.html"
            render_report(self.report(), path)
            html = path.read_text()
        self.assertIn("No closed execution evidence", html)
        self.assertNotIn("1.5000", html)
        self.assertIn("Hour equity contribution", html)
        self.assertIn("-0.37", html)
        self.assertIn("Open entry fees", html)
        self.assertIn("No lane evidence supplied", html)
        self.assertIn("No signals", html)
        self.assertIn("HTF evidence is unavailable", html)
        self.assertIn("<style>:root", html)

    def test_embedded_download_preserves_full_original_report(self):
        report = self.report()
        html = download(report, "report.json", "Download")
        payload = re.search(r"base64,([^']+)", html)[1]
        self.assertEqual(json.loads(base64.b64decode(payload)), report)

    def test_download_table_escapes_labels_and_keeps_csv_newlines(self):
        html = download([dict(symbol="A", net=-1)], "trades.csv", "<script>", csv_rows=True)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        payload = re.search(r"base64,([^']+)", html)[1]
        self.assertEqual(base64.b64decode(payload).decode(), "symbol,net\r\nA,-1\r\n")

    def test_comparison_keeps_experiment_explicit(self):
        experimental = self.report()
        experimental["profile"] = "short-history-5"
        experimental["engine"]["requiredSamples"] = 5
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "comparison.html"
            render_comparison(self.report(), experimental, path)
            html = path.read_text()
        self.assertIn("Exploratory".lower(), html)
        self.assertIn("Experimental five-position", html)
        self.assertIn("does not replace the baseline", html)


if __name__ == "__main__":
    unittest.main()
