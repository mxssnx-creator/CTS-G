"""Offline parity / volume contracts. Does not load credentials or call a venue."""
import sys,json,time,subprocess,unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'server/pulse'))
import ported_indications as p
from set_engine import pack_signals,bar_signal
from indication_extra import known,INDICATORS
from relational_volume import factor,set_factor
class Ported(unittest.TestCase):
 def test_pending_completed_bar_is_retried_without_waiting_for_next_minute(self):
  import pulse_trader as pt
  pulse=pt.Pulse.__new__(pt.Pulse);pulse.open={};pulse._scope_choice=lambda *a,**k:(object(),'pos','')
  minute=1_700_000_040.0//60*60;bar=minute-60;pending=[True];calls=[]
  def calculate(*args):
   calls.append(args)
   if len(calls)==1:return []
   pending[0]=False;return [('extra.ao__inverse-known-ema-9-21__t1',1,1.0,'ready')]
  pulse.sets=SimpleNamespace(bar_t={'X-USDT':bar},entry_signals=calculate,
                            entry_signals_pending=lambda *a:pending[0])
  with patch.object(pt,'SYMBOLS',['X-USDT']):
   self.assertEqual(pulse.set_entry_candidates(minute+1),{})
   self.assertIn('X-USDT',pulse.set_entry_candidates(minute+2))
   self.assertIn('X-USDT',pulse.set_entry_candidates(minute+3))
  self.assertEqual(len(calls),2,'ready signals are calculated once per minute')
 def test_live_stage_context_and_source_unavailability(self):
  name='ao__inverse-known-ema-9-21__t1';rows=[[100,101,99,100,1]]*5001
  answer=SimpleNamespace(stdout=json.dumps(dict(states={name:[0]*5001},available={name:False})))
  with patch.object(p.subprocess,'run',return_value=answer) as worker:
   result=p.batch(rows,[name],1_700_000_040,symbol='LONG-HISTORY-USDT')
  self.assertEqual(len(result[name]),5001);self.assertTrue(result[name].unavailable)
  self.assertEqual(len(json.loads(worker.call_args.kwargs['input'])['bars']),5001)
 def test_replay_passes_actual_symbol_and_reference_history(self):
  sys.path.insert(0,str(ROOT/'scripts'));import stage_eval as sg
  from set_engine import SetBook
  book=SetBook();book.load(dict(stratGeneral=False,stratIndications=False,stratShortRange=False,
    extraIndicators=['ao__inverse-pair-btc-relative-20__t1'],setMinStep=3,setStepMax=3,
    setSlRatios=[1],stratTrailing=False,exitIgnoreTp=False,holdAuto=False))
  # Use a catalogued pair name, independent of a future catalog spelling change.
  pair=next(n for n in p.NAMES if n.startswith('ao__inverse-pair-btc-') and n.endswith('__t1'))
  book.load({**book.processing_overlay,'extraIndicators':[pair]})
  bars={s:[[100,101,99,100,1]]*150 for s in ['SOL-USDT','BTC-USDT']}
  clocks={s:[1_700_000_000+i*60 for i in range(150)]for s in bars};seen=[]
  def signals(rows,packs,settings,*args,**kw):
   seen.append(settings);return {pack:[(0,0,'')]*len(rows)for pack in packs}
  sg._init_replay_market(bars,clocks)
  try:
   with patch.object(sg,'make_book',return_value=book),patch.object(sg.se,'pack_signals',side_effect=signals):
    sg._symbol_job(('SOL-USDT',bars['SOL-USDT'],clocks['SOL-USDT'][0]))
  finally:sg._init_replay_market({}, {})
  self.assertEqual(seen[0]['portedSymbol'],'SOL-USDT')
  self.assertEqual([r['symbol']for r in seen[0]['portedPeers']],['BTC-USDT'])
  self.assertEqual(seen[0]['portedPeers'][0]['bars'][0]['timestamp'],clocks['BTC-USDT'][0]*1000)
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
