"""Offline parity / volume contracts. Does not load credentials or call a venue."""
import sys,json,time,subprocess,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'server/pulse'))
import ported_indications as p
from set_engine import pack_signals,bar_signal
from indication_extra import known,INDICATORS
from relational_volume import factor,set_factor
class Ported(unittest.TestCase):
 def test_source_parity_and_causality(self):
  js="import {waveBars} from './shared/cts-indications/fixtures.ts';console.log(JSON.stringify(waveBars(240)))"
  rows=json.loads(subprocess.check_output(['node','--experimental-strip-types','--input-type=module','-e',js],cwd=ROOT,text=True));bars=[[b[k] for k in ['open','high','low','close','volume']]for b in rows];base=rows[0]['timestamp']/1000+60
  names=['ao__inverse-known-ema-9-21__t1','ao__tactic-dca-bounce-8-percent-0.5-1-0.25__t1'];packs=['extra.'+n for n in names]
  out=pack_signals(bars,packs,{},base,0);self.assertTrue(any(x[0]for x in out[packs[0]]));self.assertTrue(all(known(n)for n in names));self.assertTrue(all(n not in INDICATORS for n in names),'native votes are unchanged')
  for i in [80,120,160,239]:
   live=bar_signal(bars,i,packs,{},base+i*60)
   for pack in packs:self.assertEqual(live[pack],out[pack][i])
  self.assertLessEqual(p.status()['bytes'],32*1024*1024)
 def test_weights_are_neutral_and_do_not_borrow_other_groups(self):
  c=dict(indicationType='tactic',indication='id',strategy='axis',range='micro',symbol='SOL-USDT')
  self.assertEqual(factor(None,c),1)
  for n in range(1,51):self.assertAlmostEqual(factor(dict(indicationTypes=dict(tactic=n/10)),c),n/10)
  raw=dict(indicationTypes=dict(tactic=2),strategies=dict(axis=.5),ranges=dict(micro=1.5))
  self.assertEqual(factor(raw,c),1.5);self.assertEqual(factor(raw,{**c,'range':'short'}),1)
 def test_axis_is_an_independent_strategy_weight_not_a_normal_weight_stack(self):
  from types import SimpleNamespace
  st=SimpleNamespace(pack='extra.ao__inverse-known-ema-9-21__t1',kind='base',step=3)
  raw=dict(indicationTypes=dict(inverse=2),strategies=dict(normal=5,axis=.5))
  self.assertEqual(set_factor(raw,st),10)
  self.assertEqual(set_factor(raw,st,strategy='axis'),1)
  self.assertEqual(factor(dict(ranges=dict(short=2,atr=.5)),dict(range=['short','atr','short'])),1)
if __name__=='__main__':unittest.main()
