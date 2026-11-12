"""Lossless shared features for the expensive all-family indication vote."""
from pathlib import Path
import math
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
import indication_lib as lib


def plain_true_ranges(rows):
    values=[float(rows[0][1])-float(rows[0][2])]
    for previous,current in zip(rows,rows[1:]):
        high,low,close=float(current[1]),float(current[2]),float(previous[3])
        values.append(max(high-low,abs(high-close),abs(low-close)))
    return values


def history(count=3840):
    rows=[]
    for i in range(count):
        price=100+.003*i+2*math.sin(i/11)+math.sin(i/47)
        close=price+.4*math.sin(i/3)
        rows.append([price,max(price,close)+.7,min(price,close)-.4,close,10+(i*7)%19])
    return rows


class Features(unittest.TestCase):
    def setUp(self):lib._tls.memo={}
    def tearDown(self):lib._tls.memo={}

    def test_owned_aggregates_share_true_ranges_but_mutable_inputs_are_recomputed(self):
        rows=history(300);aggregate=lib._agg(rows,5)
        first=lib._tr(aggregate)
        self.assertEqual(first,plain_true_ranges(aggregate))
        self.assertIs(first,lib._tr(aggregate))
        rows[-1][1]+=10
        fresh=lib._agg(rows,5)
        self.assertIsNot(fresh,aggregate)
        self.assertEqual(lib._tr(fresh),plain_true_ranges(fresh))
        self.assertNotEqual(lib._tr(fresh),first)
        original=lib._tr(rows);rows[-1][1]+=10
        self.assertNotEqual(lib._tr(rows),original)

    def test_every_library_name_matches_the_uncached_reference_on_distinct_frames(self):
        rows=history();times=[]
        for size in(3600,3720,3840):
            frame=rows[:size];lib._tls.memo={};clock=time.perf_counter()
            with patch.object(lib,'_tr',plain_true_ranges):
                before={name:fn(frame)for name,fn in lib.LIB.items()}
            times.append(('plain',time.perf_counter()-clock))
            lib._tls.memo={};clock=time.perf_counter()
            after={name:fn(frame)for name,fn in lib.LIB.items()}
            times.append(('shared',time.perf_counter()-clock))
            self.assertEqual(before,after)
        print({'names':len(lib.LIB),'frames':3,'exactResults':len(lib.LIB)*3,'seconds':times},flush=True)


if __name__=='__main__':unittest.main(verbosity=2)
