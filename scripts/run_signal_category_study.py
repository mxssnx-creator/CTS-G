"""Freeze web-source categories, wide trailing and historical selection before scoring.

No best-outcome tuning: every arm is retained, including zero selections/losses.
This archived day has been examined previously, so it is not untouched OOS.
"""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feed",required=True)
    ap.add_argument("--start",required=True,type=int)
    ap.add_argument("--out-dir",type=Path,required=True)
    args=ap.parse_args()
    args.out_dir.mkdir(parents=True,exist_ok=False)
    full=json.loads((ROOT/"presets/signals-offline-categories.json").read_text())
    cases={name:copy.deepcopy(full) for name in ("existing-six", "nine-dynamic", "nine-frozen", "new-three-frozen", "wide-dynamic", "wide-frozen", "nine-stress")}
    cases["existing-six"]["sources"]=full["sources"][:6]
    cases["new-three-frozen"]["sources"]=full["sources"][6:]
    cases["new-three-frozen"]["protections"]=json.loads((ROOT/"presets/signals-offline-expanded.json").read_text())["protections"]
    wide=[{"id":f"trail-only-gap-{gap*100:g}pct","kind":"trailing-only","tp":0,"sl":.01,
           "trailArm":gap+.005,"trailGap":gap,"holdMinutes":120} for gap in (.005,.01,.02,.03,.05)]
    for name in ("wide-dynamic","wide-frozen"):
        cases[name]["protections"]=copy.deepcopy(wide)
    cases["nine-stress"]["fees"].update(taker=.0008,slippagePerLeg=.00015)
    for name,cfg in cases.items():
        (args.out_dir/(name+".config.json")).write_text(json.dumps(cfg,sort_keys=True,indent=2)+"\n")
    def run(name):
        command=[sys.executable,"-B",str(ROOT/"scripts/run_signal_research.py"),"--feed",args.feed,"--start",str(args.start),
                 "--config",str(args.out_dir/(name+".config.json")),"--out",str(args.out_dir/(name+".json.gz"))]
        if name.endswith("-frozen"):command.append("--freeze-prehistory")
        result=subprocess.run(command,capture_output=True,text=True,timeout=180)
        if result.returncode:raise RuntimeError(name+": "+result.stderr)
        return {"name":name,"frozenPrehistory":name.endswith("-frozen"),**json.loads(result.stdout)}
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(run,cases))
    summary={"protocol":"eight symbols; exactly 12h prehistory + 12h trading; PF>=2.2; preserve all seven arms",
             "selection":"frozen arms use only strictly earlier exact Base/Main/Real closes; all ongoing gates retained",
             "status":"retrospective diagnostic, not untouched OOS or production-approved", "results":results}
    (args.out_dir/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
    print(json.dumps(summary,indent=2))


if __name__=="__main__":main()
