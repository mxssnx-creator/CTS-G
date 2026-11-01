"""Offline Signals contracts in the standard regression entry point."""
import os
import subprocess
import sys
from common import ROOT


def contracts():
    run = subprocess.run([sys.executable,"-B",os.path.join(ROOT,"scripts/test_signal_replay.py")],
                         capture_output=True,text=True,timeout=60)
    return run.returncode == 0, (run.stderr+run.stdout).strip()[-300:]


def controls():
    run = subprocess.run([sys.executable,"-B",os.path.join(ROOT,"scripts/test_control_coverage.py")],
                         capture_output=True,text=True,timeout=60)
    return run.returncode == 0, (run.stderr+run.stdout).strip()[-300:]


def source_math():
    run = subprocess.run([sys.executable,"-B",os.path.join(ROOT,"scripts/test_signal_source_math.py")],
                         capture_output=True,text=True,timeout=60)
    return run.returncode == 0, (run.stderr+run.stdout).strip()[-300:]


def window_lanes():
    run = subprocess.run([sys.executable,"-B",os.path.join(ROOT,"scripts/test_signal_progress.py")],
                         capture_output=True,text=True,timeout=120)
    return run.returncode == 0, (run.stderr+run.stdout).strip()[-300:]


def catalogue():
    run = subprocess.run(["node","--test",os.path.join(ROOT,"scripts/test_signal_events.mjs")],
                         capture_output=True,text=True,timeout=120)
    return run.returncode == 0, (run.stderr+run.stdout).strip()[-300:]


def demo_safety():
    run = subprocess.run([sys.executable,"-B",os.path.join(ROOT,"scripts/test_vst_qa_safety.py")],
                         capture_output=True,text=True,timeout=60)
    return run.returncode == 0, (run.stderr+run.stdout).strip()[-300:]


CHECKS = [("signals.independent-replay-contracts", contracts), ("trader.overall-control-coverage", controls),
          ("signals.independent-source-math", source_math), ("signals.multiple-window-lanes", window_lanes),
          ("signals.complete-source-catalogue", catalogue), ("trader.x02-demo-safety", demo_safety)]
