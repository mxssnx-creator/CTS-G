"""Relational entry volume weights. Never alter unit returns or qualification."""
import json, math, re
BUCKETS=('indicationTypes','indications','strategies','ranges','symbols')
def settings(raw):
 out=dict(enabled=True,overall=1.0,maxMultiplier=25.0,**{b:{} for b in BUCKETS})
 if isinstance(raw,str):
  try:raw=json.loads(raw)
  except (TypeError,ValueError):return out
 if not isinstance(raw,dict):return out
 if isinstance(raw.get('enabled'),bool):out['enabled']=raw['enabled']
 def ratio(v,lo=.1,hi=5):return type(v) in (int,float) and math.isfinite(v) and lo<=v<=hi and abs(v*10-round(v*10))<1e-8
 for k in ('overall','maxMultiplier'):
  if ratio(raw.get(k),1 if k=='maxMultiplier' else .1,25 if k=='maxMultiplier' else 5):out[k]=raw[k]
 for b in BUCKETS:
  values=raw.get(b)
  if not isinstance(values,dict) or len(values)>4096:continue
  for k,v in values.items():
   key=str(k).strip().lower()
   if 0<len(key)<=200 and key not in ('__proto__','prototype','constructor') and re.fullmatch(r'[a-z0-9_.:@|/#+~\-]+',key) and ratio(v) and key not in out[b]:out[b][key]=v
 return out
def factor(raw,context):
 s=settings(raw)
 if not s['enabled']:return 1.0
 result=s['overall']
 for bucket,key in (('indicationTypes','indicationType'),('indications','indication'),('strategies','strategy'),('ranges','range'),('symbols','symbol')):
  value=context.get(key)
  if value is not None:result*=s[bucket].get(str(value).strip().lower(),1.0)
 return min(s['maxMultiplier'],max(.000001,result))
def set_factor(raw,st,symbol=''):
 from set_engine import pack_group, SetBook
 from indication_config import source_name
 pack=str(getattr(st,'pack','') or '')
 kind=str(getattr(st,'kind','base') or 'base')
 source=source_name(pack.removeprefix('extra.'))
 indication=source
 if source.startswith('ao__'):indication=source[4:].rsplit('__t',1)[0]
 indication_type='inverse' if indication.startswith('inverse-') else 'tactic' if indication.startswith('tactic-') else pack_group(pack) or pack
 return factor(raw,dict(indicationType=indication_type,indication=indication,strategy={'base':'normal','trail':'trailing'}.get(kind,kind),range=SetBook.processing_range(st),symbol=symbol))

def strategy_weight(raw,strategy):
 s=settings(raw)
 return s['strategies'].get(strategy,1.0) if s['enabled'] else 1.0
