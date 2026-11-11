"""Freeze diagnostic cost/risk scenarios before scoring. Not optimization/holdout."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed",required=True)
    ap.add_argument("--start",required=True,type=int)
    ap.add_argument("--out-dir",type=Path,required=True)
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True,exist_ok=False)
    expanded = json.loads((ROOT/"presets/signals-offline-expanded.json").read_text())
    cases = {"baseline":json.loads((ROOT/"presets/signals-offline-research.json").read_text()),
             "expanded":expanded,"cost-stress":copy.deepcopy(expanded),
             "maker-hypothesis":copy.deepcopy(expanded),"smaller-book":copy.deepcopy(expanded)}
    cases["cost-stress"]["fees"].update(taker=.0008,slippagePerLeg=.00015)
    cases["maker-hypothesis"]["fees"].update(entryLiquidity="maker")
    cases["smaller-book"]["risk"].update(notionalPerOrder=12.5,maxOpen=2,maxGrossExposure=25,maxDrawdown=.005,maxHourlyLoss=.001)
    for name,cfg in cases.items():
        (args.out_dir/(name+".config.json")).write_text(json.dumps(cfg,sort_keys=True,indent=2)+"\n")
    def run(name):
        command = [sys.executable,"-B",str(ROOT/"scripts/run_signal_research.py"),"--feed",args.feed,"--start",str(args.start),
                   "--config",str(args.out_dir/(name+".config.json")),"--out",str(args.out_dir/(name+".json.gz"))]
        process = subprocess.run(command,capture_output=True,text=True,timeout=120)
        if process.returncode: raise RuntimeError(process.stderr)
        return json.loads(process.stdout)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(run,cases))
    summary = {"purpose":"retrospective cost/risk diagnostics; no untouched holdout, no preset promoted", "results":results}
    (args.out_dir/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))


if __name__=="__main__":main()
