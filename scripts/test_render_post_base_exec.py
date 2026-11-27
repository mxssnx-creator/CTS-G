"""Accounting checks for the published post-Base research report."""
import copy
import os
from pathlib import Path
import subprocess
import sys
import unittest

from render_post_base_exec import validate_period


def period():
    group = {"closed": 2, "held": 1, "gain": 0.03, "loss": 0.02,
             "net": 0.01, "adds": 1}
    return {"hours": 2, "cases": [{
        "opened": 3, **group, "pf": 1.5, "positiveHours": 1,
        "hourSuccessPct": 50.0,
        "hourly": [
            {"opened": 1, "closed": 1, "gain": 0.03, "loss": 0.0, "net": 0.03, "adds": 1},
            {"opened": 2, "closed": 1, "gain": 0.0, "loss": 0.02, "net": -0.02, "adds": 0},
        ],
        "paths": [[0, 0, 0, dict(group)]], "bySymbol": [["TEST", dict(group)]],
    }]}


class ReportAccountingTests(unittest.TestCase):
    def test_reconciliation_detects_misreported_views(self):
        data = period()
        validate_period(data)
        for mutate in (
            lambda row: row["hourly"][0].update(opened=2),
            lambda row: row["paths"][0][3].update(loss=0.01),
            lambda row: row["bySymbol"][0][1].update(closed=1),
            lambda row: row.update(pf=2.0),
            lambda row: row.update(positiveHours=2),
        ):
            damaged = copy.deepcopy(data)
            mutate(damaged["cases"][0])
            with self.assertRaises(ValueError):
                validate_period(damaged)

    def test_offline_replay_help_does_not_require_a_proxy_transport(self):
        env = dict(os.environ, ALL_PROXY="socks5://127.0.0.1:9")
        runner = Path(__file__).with_name("post_base_exec.py")
        completed = subprocess.run([sys.executable, str(runner), "--help"], env=env,
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--base-floor", completed.stdout)


if __name__ == "__main__":
    unittest.main()
