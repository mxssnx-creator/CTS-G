"""The Python-source event export must preserve causal clocks and identities."""
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "scripts/export_signal_python_events.py"
T = 1791072000000


class Export(unittest.TestCase):
    def test_completed_bar_warmup_and_gaps(self):
        rows = [{"t": T+i*60000, "o": 100+i*.1, "h": 100.1+i*.1,
                 "l": 99.9+i*.1, "c": 100+i*.1, "v": 2} for i in range(80)]
        with tempfile.TemporaryDirectory() as td:
            feed, out = Path(td)/"feed.json.gz", Path(td)/"events.json.gz"
            feed.write_bytes(gzip.compress(json.dumps({"candles": {"A-USDT|1": rows}}).encode(), mtime=0))
            args = [sys.executable, str(EXPORT), "--feed", str(feed), "--out", str(out),
                    "--start", str(T), "--end", str(T+80*60000), "--symbols", "A-USDT",
                    "--sources", "volume-momentum", "--timeframes", "1"]
            subprocess.run(args, check=True, capture_output=True, text=True)
            archive = json.loads(gzip.decompress(out.read_bytes()))
            self.assertEqual(archive["provenance"]["feedSha256"], hashlib.sha256(feed.read_bytes()).hexdigest())
            self.assertEqual([s["identity"]["sourceRange"] for s in archive["series"]], ["short", "medium"])
            self.assertEqual(archive["series"][0]["events"], [[T+21*60000, 1]])
            self.assertEqual(archive["series"][1]["events"], [[T+51*60000, 1]])
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0, "do not overwrite an archive")
            out.unlink()
            feed.write_bytes(gzip.compress(json.dumps({"candles": {"A-USDT|1": rows[:20]+rows[21:]}}).encode(), mtime=0))
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
