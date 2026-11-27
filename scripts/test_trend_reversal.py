from pathlib import Path
import json
import math
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
from indication_extra import known
from signal_packs import pack_name, signal
from signal_sources import onsets
from trend_reversal import PERIODS, TACTICS, reversal_at

class ReversalTest(unittest.TestCase):
    def test_reference_batch_and_latest_signals(self):
        close, seed = 100, 291
        rows = []
        def random():
            nonlocal seed
            seed = (1664525 * seed + 1013904223) % 2**32
            return seed / 2**32
        for i in range(420):
            o = close
            close += math.sin(i/3)*.23+(random()-.5)*1.2
            rows.append([1790000040000+i*60000, o, max(o,close)+random()*.25, min(o,close)-random()*.25, close, 5+random()*40])
        js = """
import {readFileSync} from "node:fs";
import {REVERSAL_SOURCES,trendReversalSeries} from "./shared/cts-reversal/reversal.ts";
const rows=JSON.parse(readFileSync(0,"utf8"));
const [t,o,h,l,c,v]=rows[0].map((_,i)=>rows.map(r=>r[i]));
console.log(JSON.stringify(Object.fromEntries(REVERSAL_SOURCES.map(s=>[s.id,[...trendReversalSeries({t,o,h,l,c,v},s.period,s.tactic,60000)]]))));
"""
        oracle = json.loads(subprocess.check_output(
            ["node", "--experimental-strip-types", "--input-type=module", "-e", js], cwd=ROOT,
            input=json.dumps(rows), text=True))
        raw = [r[1:] for r in rows]
        for tactic in TACTICS:
            for period in PERIODS:
                key=f"trend-reversal-{tactic}-{period}"
                self.assertTrue(known(f"trrev{tactic}_n{period}_t1"))
                self.assertEqual([reversal_at(raw,i,period,tactic) for i in range(len(rows))],oracle[key])
            source=f"trend-reversal-{tactic}"
            pack=pack_name(source,"short",1)
            events=onsets(rows,source,"short",1)
            live={row[0]+60000: direction for i,row in enumerate(rows)
                  if (direction:=signal(raw,i,(row[0]+60000)/1000,pack)[0])}
            self.assertEqual(live,events)
if __name__ == "__main__":
    unittest.main()
