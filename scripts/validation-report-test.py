#!/usr/bin/env python3
"""Static artifact checks; does not claim a browser/visual acceptance."""
import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None
        self.external = []
        self.body_rows = 0
        self.in_configs = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            if attrs.get("src"):
                self.external.append(attrs["src"])
            self.current = {"type": attrs.get("type", "javascript"), "text": ""}
        if tag == "table" and attrs.get("id") == "configs":
            self.in_configs = True
        if tag == "tr" and self.in_configs:
            self.body_rows += 1

    def handle_endtag(self, tag):
        if tag == "script" and self.current:
            self.scripts.append(self.current)
            self.current = None
        if tag == "table":
            self.in_configs = False

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "reports/cts-g-validation.html"
        cls.html = cls.path.read_text()
        cls.parsed = ReportParser()
        cls.parsed.feed(cls.html)
        cls.data = json.loads(next(s["text"] for s in cls.parsed.scripts if s["type"] == "application/json"))

    def test_entire_unique_grid_embedded(self):
        self.assertEqual(len(self.data["rows"]), 3888)
        self.assertEqual(len({row["id"] for row in self.data["rows"]}), 3888)
        self.assertEqual({row["symbol"] for row in self.data["rows"]}, {"SOL-USDT", "XRP-USDT", "BCH-USDT"})

    def test_diagnostics_for_every_configuration(self):
        for row in self.data["rows"]:
            self.assertEqual(set(row["recentPf"]), {"last8", "last25", "last75"})
            self.assertGreaterEqual(row["avgDdS"], 0)
            for key, window in row["recentPf"].items():
                self.assertEqual(window["available"], window["n"] >= int(key[4:]))

    def test_bounded_first_render_and_file_size(self):
        self.assertEqual(self.parsed.body_rows, 101)  # header + 100 rows
        self.assertLess(self.path.stat().st_size, 4 * 1024 * 1024)
        self.assertEqual(self.parsed.external, [])

    def test_interactive_script_syntax(self):
        for script in self.parsed.scripts:
            if script["type"] == "application/json":
                continue
            result = subprocess.run(["node", "--check", "-"], input=script["text"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_evidence_limitations_visible(self):
        for text in ("Keine Mainnet-Freigabe", "keine gemessenen Kontogebühren", "BLOCKIERT",
                     "keine visuelle", "unvollständig", "kein Kontoertrag"):
            self.assertIn(text.casefold(), self.html.casefold())


if __name__ == "__main__":
    unittest.main(verbosity=2)
