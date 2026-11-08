#!/usr/bin/env python3
"""50-symbol / 10-minute VST conformance test, with production signal decisions in shadow.

Execution probes deliberately cover every SL ratio and the lowest three range steps.
They are NOT PF-qualified strategy admissions or a proof of strategy profitability.
Only VST is supported. No leverage/mode changes, mainnet or account-wide close/cancel.
Every write is journaled; cleanup is restricted to this run's empty-preflight symbols.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import hashlib
import math
import re
from pathlib import Path
import sys
import time

from x02_demo_acceptance import Acceptance, Client, HOST, ORDER
from fee_policy import cost_policy, paper_cost_pct
from position_cost import SL_RATIOS, sl_floor_frac
from pulse_trader import ctrl_payload
from set_engine import SetBook, bar_signal


class ObservationDeadline(BaseException):
    """The bounded demo observation ended; finish owned cleanup, not another replay."""
    # This control-flow cancellation must escape replay_all's per-symbol
    # Exception isolation, just as an operator interrupt does.


def batch_payload(bodies):
    """The batch JSON schema uses numbers; the single-order query uses text."""
    if not 1 <= len(bodies) <= 5:
        raise ValueError('VST batch contains 1..5 orders')
    encoded=[]
    for body in bodies:
        row=dict(body)
        for key in ('quantity','quoteOrderQty','price','stopPrice','activationPrice','priceRate'):
            if key in row:
                value=float(row[key])
                if not math.isfinite(value) or value<=0:
                    raise ValueError('Invalid positive batch number: '+key)
                row[key]=value
        encoded.append(row)
    return json.dumps(encoded,separators=(',',':'),allow_nan=False)


def stop_prices(entry, side, step, ratio, cost_pct, net_pct, precision):
    sign=1 if side=='LONG' else -1
    tp_frac=step*.001
    sl_frac=max(tp_frac*ratio,sl_floor_frac(cost_pct,min_distance_pct=net_pct))
    tick=Decimal(10)**-precision
    sl=(Decimal(str(entry))*(1-Decimal(str(sign*sl_frac)))).quantize(tick,rounding=ROUND_FLOOR if sign>0 else ROUND_CEILING)
    tp=(Decimal(str(entry))*(1+Decimal(str(sign*tp_frac)))).quantize(tick,rounding=ROUND_CEILING if sign>0 else ROUND_FLOOR)
    if min(sl,tp)<=0:raise ValueError('Protection outside positive prices')
    return str(sl),str(tp),sl_frac,tp_frac


def crossed_protection_error(error):
    # TP and SL each have a separate code for either side of the current mark.
    # Match the documented rejection itself; unrelated/unknown failures still abort.
    return bool(re.search(r'VST API 11041[1-4]: (?:Stop Loss|Take Profit) price should be (?:greater|lower|less|higher) than the current price', str(error)))


def plan_for(contracts,tickers,count=50):
    specs={x['symbol']:x for x in contracts if x.get('status')==1 and str(x.get('apiStateOpen')).lower()=='true' and str(x.get('apiStateClose')).lower()=='true'}
    ranked=sorted(tickers,key=lambda x:-float(x.get('quoteVolume') or 0))
    rows=[]
    for tick in ranked:
        sym=tick['symbol'];c=specs.get(sym)
        if not c or not sym.endswith('-USDT') or sym.startswith(('NCSK','NCCO','NCSI')):continue
        px=float(tick.get('lastPrice') or 0)
        if px<=0:continue
        # Contract "size" is value per contract, not the quantity increment.
        lot=Decimal(10)**-int(c['quantityPrecision'])
        need=max(Decimal(str(c['tradeMinQuantity'])),Decimal(str(c['tradeMinUSDT']))*Decimal('1.05')/Decimal(str(px)))
        q=(need/lot).to_integral_value(rounding=ROUND_CEILING)*lot
        # At most three minimum lots, at most 30 VST notional per symbol.
        if float(q)*px*3>30:continue
        if 10**(-int(c['pricePrecision']))/px > .0002:continue
        i=len(rows)
        rows.append(dict(symbol=sym,side='LONG' if i%2==0 else 'SHORT',quantity=str(q),maxQty=float(q*3),
                         price=px,pricePrecision=int(c['pricePrecision']),quantityPrecision=int(c['quantityPrecision']),
                         step=(2,4,7)[i%3],ratio=SL_RATIOS[i%len(SL_RATIOS)],quoteVolume=float(tick.get('quoteVolume') or 0)))
        if len(rows)==count:break
    if len(rows)!=count:raise RuntimeError('Fewer than 50 eligible VST contracts')
    return rows


class Session(Acceptance):
    def __init__(self,client,out):
        super().__init__(client,out)
        self.report.update(test='controlled lowest-step execution probes + shadow production decisions',
            profitabilityClaim=False,limitations=['REST sampled mark prices are not a full tick replay',
            'Probe entries cover geometry; they do not bypass or validate production PF gates'],snapshots=[],shadow=[])
        root=Path(__file__).parents[1]
        self.report['sourceSha256']={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
            ('scripts/x02_low_steps_session.py','scripts/x02_demo_acceptance.py','server/pulse/set_engine.py',
             'server/pulse/position_cost.py','server/pulse/pulse_trader.py','server/pulse/protection-policy.json')}
        self.last_write=0;self._last_sample=time.monotonic()
        self.book=SetBook()

    def write(self,method,path,body):
        # Serialize demo writes below one request per second, including older regional rate limits.
        time.sleep(max(0,1.05-(time.monotonic()-self.last_write)))
        self.last_write=time.monotonic()
        return super().write(method,path,body)

    def candles(self,row,limit=600):
        raw=self.api.call('GET','/openApi/swap/v3/quote/klines',dict(symbol=row['symbol'],interval='1m',limit=limit,
                         timestamp=int(time.time()*1000)),signed=False)['data']
        now=int(time.time()*1000)+self.api.offset
        rows=sorted((x for x in raw if int(x['time'])+60000<=now),key=lambda x:int(x['time']))
        if len(rows)<120:raise RuntimeError('Insufficient closed candle history: '+row['symbol'])
        return row['symbol'],rows

    def prepare(self):
        balances=self.api.rows('/openApi/swap/v3/user/balance')
        self.check('virtual-currency',any(r.get('asset')=='VST' for r in balances))
        positions=self.api.rows('/openApi/swap/v2/user/positions')
        orders=self.api.rows('/openApi/swap/v2/trade/openOrders',key='orders')
        self.check('empty-demo-account-preflight',not any(abs(float(x.get('positionAmt') or 0))>0 for x in positions) and not orders)
        self.report['initialBalances']=balances
        fees=self.api.call('GET','/openApi/swap/v2/user/commissionRate')['data']['commission']
        policy=cost_policy(dict(makerFeeRate=float(fees['makerCommissionRate']),takerFeeRate=float(fees['takerCommissionRate']),slippageFeeRatio=.1))
        self.report['feePolicy']=policy;self.cost=paper_cost_pct(policy);self.net=.1
        self.report['modeledRoundTripCostPct']=self.cost;self.report['minSlDistancePct']=self.net
        contracts=self.api.call('GET','/openApi/swap/v2/quote/contracts',signed=False)['data']
        ticks=self.api.call('GET','/openApi/swap/v2/quote/ticker',signed=False)['data']
        # Current leverage can outlive a contract's reduced maximum. Validate
        # every candidate before placing any order; leave account settings intact.
        candidates=plan_for(contracts,ticks,100)
        eligible=[];excluded=[]
        def leverage(row):
            return row,self.api.call('GET','/openApi/swap/v2/trade/leverage',{'symbol':row['symbol']})['data']
        with ThreadPoolExecutor(max_workers=3) as pool:
            for start in range(0,len(candidates),10):
                for row,limits in pool.map(leverage,candidates[start:start+10]):
                    valid=all(0<float(limits.get(side+'Leverage') or 0)<=float(limits.get('max'+side.title()+'Leverage') or 0)
                              for side in ('long','short'))
                    if valid:eligible.append(row)
                    else:excluded.append({'symbol':row['symbol'],'limits':limits})
                if len(eligible)>=50:break
        self.check('fifty-contracts-with-valid-leverage',len(eligible)>=50,eligible=len(eligible),excluded=len(excluded))
        self.report['excludedLeverage']=excluded
        self.report['plan']=eligible[:50]
        for i,row in enumerate(self.report['plan']):
            row.update(side='LONG' if i%2==0 else 'SHORT',step=(2,4,7)[i%3],ratio=SL_RATIOS[i%len(SL_RATIOS)])
        with ThreadPoolExecutor(max_workers=3) as pool:history=dict(pool.map(self.candles,self.report['plan']))
        (self.out/'candles-start.json').write_text(json.dumps(history))
        overlay=json.loads((Path(__file__).parents[1]/'server/pulse/overlay-bingx-x02.json').read_text())
        overlay.update(positionCostPct=self.cost,minSlDistancePct=self.net,holdAuto=False,histLookbackAuto=False,
                       histLookbackBars=600,setMinStep=2,setStepMax=7,setStepAdapt=False,trailSlRatios=[],
                       tpRanges={"micro":True,"minimal":True,"short":True,"normal":False,"long":False,"wide":False},
                       trailRanges={"micro":True,"minimal":True,"short":True,"normal":False,"long":False,"wide":False},
                       shortRangeStepMin=2,shortRangeStepMax=7,setSlRatios=list(SL_RATIOS),stratTrend=True,stratBreakout=True,stratShortRange=True)
        self.book.load(overlay)
        for sym,rows in history.items():
            self.book.bars[sym]=[[float(r[k]) for k in ('open','high','low','close','volume')] for r in rows]
            self.book.bar_t[sym]=int(rows[-1]['time'])/1000
        t=time.monotonic();self.book.replay_all(now=max(self.book.bar_t.values())+60)
        self.report['initialReplaySeconds']=time.monotonic()-t
        self.report['configuredSets']=len(self.book.by_idx)
        self.shadow()
        self.save()

    def shadow(self):
        decisions=[]
        for plan in self.report['plan']:
            sym=plan['symbol'];bars=self.book.bars[sym]
            votes=bar_signal(bars,len(bars)-1,self.book.packs,self.book.ind_settings,self.book.bar_t[sym]+60)
            signals=[(pk,sd,cf,why) for pk,(sd,cf,why) in votes.items() if sd and cf>=self.book.entry_conf]
            chosen=[]
            for pk,sd,cf,why in signals:
                st=self.book.pick_any(pk,side=sd)
                if st:chosen.append(dict(set=st.id,pack=pk,side=sd,confidence=cf))
            decisions.append(dict(symbol=sym,signals=len(signals),qualified=chosen))
        self.report['shadow'].append(dict(at=int(time.time()*1000),decisions=decisions));self.save()

    def protection(self,case):
        sl,tp,sl_frac,tp_frac=stop_prices(case['entry'],case['side'],case['step'],case['ratio'],self.cost,self.net,case['pricePrecision'])
        case.update(sl=float(sl),tp=float(tp),slFraction=sl_frac,tpFraction=tp_frac)
        for kind,px in (('sl',sl),('tp',tp)):
            # If an earlier control already closed this tiny position, don't create an orphan.
            qty,_=self.api.position(case['symbol'],case['side'])
            if qty<=1e-12:break
            cid=self.cid();body=ctrl_payload(case['symbol'],case['side'],kind,px,str(qty),cid,close_pos=True)
            case['controls'].append(dict(cid=cid,kind=kind,orderId='',requested=body));self.save()
            try:response=self.write('POST',ORDER,body)
            except RuntimeError as exc:
                if not crossed_protection_error(exc):raise
                self.recover_crossed_protection([case],str(exc));return
            r=response['data'];r=r.get('order',r)
            case['controls'][-1]['orderId']=str(r.get('orderId') or '')
            self.save()
        self.check('stop-floor-and-target',abs(case['sl']-case['entry'])/case['entry']+1e-12>=sl_floor_frac(self.cost,min_distance_pct=self.net)
                   and abs(abs(case['tp']-case['entry'])/case['entry']-tp_frac)<=10**-case['pricePrecision']/case['entry']+1e-12,
                   symbol=case['symbol'],step=case['step'],ratio=case['ratio'],sl=case['sl'],tp=case['tp'])

    def open_case(self,plan):
        self.clear(plan['symbol'])
        case={**plan,'controls':[],'fills':[],'cleaned':False,'simExit':None,'probe':True}
        self.report['cases'].append(case);self.save()
        q=Decimal(plan['quantity']);side=plan['side']
        fill=self.market(plan['symbol'],side,'BUY' if side=='LONG' else 'SELL',q*2)
        case['fills'].append(fill);case['entry']=float(fill['avgPrice']);case['openedAt']=int(time.time()*1000)
        self.protection(case);self.save()
        return case

    def batch(self,bodies):
        """One transport for at most five independently journaled identities. Never infer fills from position size."""
        payload=batch_payload(bodies)
        intents=[]
        for b in bodies:
            intent=dict(method='POST',path=ORDER,transportPath='/openApi/swap/v2/trade/batchOrders',
                        body=dict(b),t=int(time.time()*1000),result='pending')
            self.log.append(intent);intents.append(intent)
        self.save()
        time.sleep(max(0,1.05-(time.monotonic()-self.last_write)));self.last_write=time.monotonic()
        try:
            result=self.api.call('POST','/openApi/swap/v2/trade/batchOrders',
                                 {'batchOrders':payload})
            data=result.get('data',{});orders=data.get('orders',[]) if isinstance(data,dict) else []
            found={str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower():o for o in orders}
        except Exception as exc:
            for intent in intents:intent.update(result='unconfirmed-or-rejected',error=str(exc))
            self.save();raise
        out=[]
        for intent in intents:
            b=intent['body'];o=found.get(b['clientOrderID'].lower())
            if o and o.get('symbol')==b['symbol'] and o.get('positionSide')==b['positionSide'] and o.get('side')==b['side'] and o.get('orderId'):
                intent.update(result='accepted',response={'data':{'order':o}});out.append(o)
            else:intent.update(result='unconfirmed-or-rejected',batchErrors=data.get('errors',[]) if isinstance(data,dict) else [])
        self.save()
        if len(out)!=len(bodies):raise RuntimeError('VST batch contains unconfirmed orders; reconcile each original identity')
        return out

    def open_group(self,plans):
        cases=[];bodies=[]
        for plan in plans:
            case={**plan,'controls':[],'fills':[],'cleaned':False,'simExit':None,'probe':True}
            self.report['cases'].append(case);cases.append(case)
            bodies.append(dict(symbol=plan['symbol'],positionSide=plan['side'],side='BUY' if plan['side']=='LONG' else 'SELL',
                type='MARKET',quantity=str(Decimal(plan['quantity'])*2),clientOrderID=self.cid()))
        orders=self.batch(bodies);controls=[]
        for c,b,o in zip(cases,bodies,orders):
            if o.get('status')!='FILLED':o=self.api.order(c['symbol'],b['clientOrderID'])
            if o.get('status') in ('NEW','PARTIALLY_FILLED'):
                self.write('DELETE',ORDER,{'symbol':c['symbol'],'orderId':str(o['orderId'])})
                o=self.api.order(c['symbol'],b['clientOrderID'])
            qty=float(o.get('executedQty') or 0);entry=float(o.get('avgPrice') or 0)
            # BingX can round a valid market quantity to its actual contract lot.
            # Terminal partial fills own only executedQty, never requestedQty.
            self.check('batch-market-fill',o.get('status') in ('FILLED','CANCELED','CANCELLED','EXPIRED')
                and entry>0 and 0<qty<=float(b['quantity'])+1e-9,symbol=c['symbol'],
                requestedQty=float(b['quantity']),filledQty=qty,status=o.get('status'))
            c.update(entry=entry,openedAt=int(time.time()*1000));c['fills'].append(o)
            sl,tp,sf,tf=stop_prices(entry,c['side'],c['step'],c['ratio'],self.cost,self.net,c['pricePrecision'])
            c.update(sl=float(sl),tp=float(tp),slFraction=sf,tpFraction=tf)
            for kind,price in [('sl',sl),('tp',tp)]:
                cid=self.cid();body=ctrl_payload(c['symbol'],c['side'],kind,price,str(qty),cid,close_pos=True)
                c['controls'].append(dict(cid=cid,kind=kind,orderId='',requested=body));controls.append((c,body))
            self.check('stop-floor-and-target',abs(c['sl']-entry)/entry+1e-12>=sl_floor_frac(self.cost,min_distance_pct=self.net)
                and abs(abs(c['tp']-entry)/entry-tf)<=10**-c['pricePrecision']/entry+1e-12,symbol=c['symbol'],step=c['step'],ratio=c['ratio'])
        self.save()
        try:
            for start in range(0,len(controls),5):
                group=controls[start:start+5];accepted=self.batch([b for _,b in group])
                for (c,b),o in zip(group,accepted):
                    next(x for x in c['controls'] if x['cid']==b['clientOrderID'])['orderId']=str(o['orderId'])
                self.save()
        except RuntimeError as exc:
            if not crossed_protection_error(exc):raise
            # At tiny targets the market can cross a barrier before a separate
            # protection request arrives. Never widen that target or retry an
            # uncertain identity. Reconcile the batch, then flatten only the
            # affected owned probes; keep fully confirmed protection in place.
            self.recover_crossed_protection(cases,str(exc))

    def recover_crossed_protection(self,cases,error):
        orders=self.api.rows('/openApi/swap/v2/trade/openOrders',key='orders')
        by_id={str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower():o for o in orders}
        for c in cases:
            confirmed=all((o:=by_id.get(x['cid'].lower())) is not None
                and o.get('symbol')==c['symbol'] and o.get('positionSide')==c['side']
                and str(o.get('closePosition')).lower()=='true' and o.get('workingType')=='MARK_PRICE'
                for x in c['controls']) and len(c['controls'])==2
            if confirmed:continue
            c['protectionDispatchFailure']=dict(error=error,at=int(time.time()*1000),action='flatten-owned-probe')
            self.save();self.cleanup(c)

    def sample(self):
        prices=self.api.call('GET','/openApi/swap/v2/quote/premiumIndex',{'timestamp':int(time.time()*1000)},signed=False)['data']
        if not isinstance(prices,list):raise RuntimeError('Mark-price bulk response unknown')
        marks={p['symbol']:float(p['markPrice']) for p in prices}
        positions=self.api.rows('/openApi/swap/v2/user/positions')
        orders=self.api.rows('/openApi/swap/v2/trade/openOrders',key='orders')
        bypos={(p['symbol'],p['positionSide']):p for p in positions if abs(float(p.get('positionAmt') or 0))>0}
        snap={'at':int(time.time()*1000),'positions':[], 'pendingControls':0}
        own={r['cid'].lower() for c in self.report['cases'] for r in c['controls']}
        snap['pendingControls']=sum(str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower() in own for o in orders)
        orders_by_cid={str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower():o for o in orders}
        snap['protectedPositions']=0
        for c in self.report['cases']:
            p=bypos.get((c['symbol'],c['side']));m=marks.get(c['symbol'])
            if m is None:continue
            q=abs(float(p.get('positionAmt') or 0)) if p else 0
            if q>c['maxQty']+1e-9:raise RuntimeError('Concurrent exposure exceeds own journal')
            if q>0:
                covered=all((o:=orders_by_cid.get(x['cid'].lower())) is not None and o.get('symbol')==c['symbol']
                    and o.get('positionSide')==c['side'] and str(o.get('closePosition')).lower()=='true'
                    and o.get('workingType')=='MARK_PRICE' for x in c['controls']) and len(c['controls'])==2
                if not covered:
                    q,_=self.api.position(c['symbol'],c['side'])
                    if q>0:raise RuntimeError('An owned open demo position lacks confirmed SL and TP')
                else:snap['protectedPositions']+=1
            snap['positions'].append(dict(symbol=c['symbol'],side=c['side'],mark=m,quantity=q,unrealized=float(p.get('unrealizedProfit') or 0) if p else 0))
            if c['simExit'] is None:
                sd=1 if c['side']=='LONG' else -1
                if sd*(m-c['sl'])<=0 or sd*(m-c['tp'])>=0:
                    reason='sl' if sd*(m-c['sl'])<=0 else 'tp'
                    c['simExit']=dict(at=snap['at'],reason=reason,price=c[reason],sampleMark=m)
        self.report['snapshots'].append(snap);self.save()
        print(json.dumps({'elapsedSeconds':round((snap['at']-self.report.get('activeStartedAt',snap['at']))/1000),
                          'symbols':len(self.report['plan']),'cases':len(self.report['cases']),
                          'openPositions':len(bypos),'ownControls':snap['pendingControls']}),flush=True)
        # Measure the quiet interval after IO completes. Slow REST requests
        # otherwise make every replay callback immediately sample again.
        self._last_sample=time.monotonic()

    def monitor_replay(self,deadline):
        if time.monotonic()>=deadline:raise ObservationDeadline()
        if time.monotonic()-self._last_sample>=10:self.sample()
        if time.monotonic()>=deadline:raise ObservationDeadline()

    def partial_probes(self):
        # One additional lot and one partial close on a few still-open lanes; overall controls retain ownership.
        for c in self.report['cases'][:6]:
            qty,_=self.api.position(c['symbol'],c['side'])
            if qty<=0:continue
            q=Decimal(c['quantity']);opening='BUY' if c['side']=='LONG' else 'SELL'
            f=self.market(c['symbol'],c['side'],opening,q);c['fills'].append(f);self.save()
            total,_=self.api.position(c['symbol'],c['side'])
            if total>0:
                c['fills'].append(self.market(c['symbol'],c['side'],'SELL' if opening=='BUY' else 'BUY',min(float(q),total)));self.save()
            c['partialProbe']=True

    def cleanup_all(self):
        """Bulk reads and five-order closes; uncertain orders use the slower identity reconciliation."""
        failures=[]
        try:
            market=[r for r in self.log if r['method']=='POST' and r['body'].get('type')=='MARKET']
            def terminal(r):
                ack=r.get('response',{}).get('data',{});ack=ack.get('order',ack)
                o=ack if r.get('result')=='accepted' and ack.get('status')=='FILLED' else self.api.order(r['body']['symbol'],r['body']['clientOrderID'])
                if o.get('status') not in ('FILLED','CANCELED','CANCELLED','EXPIRED','REJECTED'):
                    raise RuntimeError('Market intent unresolved before bulk cleanup')
            with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(terminal,market))
            positions=self.api.rows('/openApi/swap/v2/user/positions')
            batches=[]
            for c in self.report['cases']:
                qty=sum(abs(float(p.get('positionAmt') or 0)) for p in positions
                    if p.get('symbol')==c['symbol'] and p.get('positionSide')==c['side'])
                if qty>c['maxQty']+1e-9:raise RuntimeError('Concurrent exposure: refusing bulk cleanup')
                if qty:batches.append((c,dict(symbol=c['symbol'],positionSide=c['side'],
                    side='SELL' if c['side']=='LONG' else 'BUY',type='MARKET',quantity=str(qty),clientOrderID=self.cid())))
            for start in range(0,len(batches),5):
                group=batches[start:start+5];orders=self.batch([b for _,b in group])
                for (c,b),o in zip(group,orders):
                    c['fills'].append(o)
                    if o.get('status')!='FILLED' or abs(float(o.get('executedQty') or 0)-float(b['quantity']))>1e-9:
                        raise RuntimeError('Bulk close needs original-identity reconciliation')
                self.save()
            positions=self.api.rows('/openApi/swap/v2/user/positions')
            own={(c['symbol'],c['side']) for c in self.report['cases']}
            if any((p.get('symbol'),p.get('positionSide')) in own and abs(float(p.get('positionAmt') or 0))>1e-12 for p in positions):
                raise RuntimeError('Owned remainder after bulk close')
            ids={str(r['body'].get('clientOrderID','')).lower() for r in self.log if r['method']=='POST'}
            orders=self.api.rows('/openApi/swap/v2/trade/openOrders',key='orders')
            for o in orders:
                if str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower() in ids:
                    self.write('DELETE',ORDER,dict(symbol=o['symbol'],orderId=str(o['orderId'])))
            for c in self.report['cases']:c['cleaned']=True
            self.save()
        except Exception as exc:
            self.report['bulkCleanupFallback']=str(exc);self.save()
            for c in self.report['cases']:
                try:
                    if not c.get('cleaned'):self.cleanup(c)
                except Exception as error:failures.append({'symbol':c['symbol'],'error':str(error)})
        return failures

    def finish_stats(self):
        rows=[];rate=self.report['feePolicy']['takerFeeRate'];unresolved=[]
        intents={}
        for r in self.log:
            b=r['body'];cid=b.get('clientOrderID')
            if r['method']=='POST' and cid:intents[(b['symbol'],cid.lower())]=r
        def resolve(item):
            key,r=item;ack=r.get('response',{}).get('data',{});ack=ack.get('order',ack)
            if ack.get('status')=='FILLED':return key,ack,None
            try:return key,self.api.order(key[0],key[1]),None
            except RuntimeError as exc:return key,None,str(exc)
        with ThreadPoolExecutor(max_workers=3) as pool:
            resolved=list(pool.map(resolve,intents.items()))
        by_id={key:o for key,o,error in resolved if not error}
        for key,o,error in resolved:
            if error:unresolved.append({'symbol':key[0],'clientOrderID':key[1],'error':error})
        for c in self.report['cases']:
            orders=[];seen=set()
            for r in self.log:
                b=r['body']
                if r['method']!='POST' or b.get('symbol')!=c['symbol'] or b.get('positionSide')!=c['side']:continue
                cid=b.get('clientOrderID')
                if not cid or cid in seen:continue
                seen.add(cid)
                o=by_id.get((c['symbol'],cid.lower()))
                if not o:continue
                if float(o.get('executedQty') or 0)>0:orders.append(o)
            signed=sum((1 if o['side']=='SELL' else -1)*float(o['executedQty'])*float(o['avgPrice']) for o in orders)
            fee=sum(abs(float(o['executedQty'])*float(o['avgPrice']))*rate for o in orders)
            opening='BUY' if c['side']=='LONG' else 'SELL'
            entry_notional=sum(float(o['executedQty'])*float(o['avgPrice']) for o in orders if o['side']==opening)
            qty_delta=sum((1 if o['side']==opening else -1)*float(o['executedQty']) for o in orders)
            complete=not any(x['symbol']==c['symbol'] for x in unresolved) and abs(qty_delta)<=1e-9 and c.get('cleaned',False)
            # Report fees from the account's rate as estimates; actual ledger income is retained separately.
            rows.append(dict(symbol=c['symbol'],side=c['side'],step=c['step'],ratio=c['ratio'],filledOrders=len(orders),
                             grossCashFlow=signed if complete else None,estimatedFee=fee if complete else None,
                             netEstimate=signed-fee if complete else None,entryNotional=entry_notional,
                             netReturnEstimate=(signed-fee)/entry_notional if complete and entry_notional else None,
                             feeSource='account-rate estimate',accountingComplete=complete,unmatchedQuantity=qty_delta,
                             simExit=c['simExit'],cleaned=c['cleaned'],orders=orders))
        self.report['settlements']=rows
        self.report['unresolvedAccountingOrders']=unresolved
        complete=all(x['accountingComplete'] for x in rows)
        gp=sum(max(0,x['netEstimate'] or 0) for x in rows);gl=-sum(min(0,x['netEstimate'] or 0) for x in rows)
        rg=sum(max(0,x['netReturnEstimate'] or 0) for x in rows);rl=-sum(min(0,x['netReturnEstimate'] or 0) for x in rows)
        self.report['executionProbeStats']=dict(symbols=len(rows),filledOrders=sum(r['filledOrders'] for r in rows),
                     accountingComplete=complete,netEstimate=sum(r['netEstimate'] or 0 for r in rows) if complete else None,
                     cashPf=gp/gl if complete and gl else None,returnPf=rg/rl if complete and rl else None,
                     grossProfit=gp if complete else None,grossLoss=gl if complete else None,
                     interpretation='controlled execution probes; not strategy-qualified PF')
        try:self.report['ledgerIncome']=self.api.call('GET','/openApi/swap/v2/user/income',dict(startTime=self.report['startedUtcMs'],limit=1000))['data']
        except RuntimeError as exc:self.report['ledgerError']=str(exc)
        self.report['finalBalances']=self.api.rows('/openApi/swap/v3/user/balance');self.save()

    def run(self,execute):
        self.save();self.prepare()
        if not execute:
            self.report.update(complete=True,executed=False);self.save();return
        self.report['activeStartedAt']=int(time.time()*1000);self.save()
        try:
            # Refresh the account-wide empty preflight after the historic replay; no other session may be running.
            self.check('empty-before-dispatch',not self.api.rows('/openApi/swap/v2/trade/openOrders',key='orders')
                and not any(abs(float(p.get('positionAmt') or 0))>0 for p in self.api.rows('/openApi/swap/v2/user/positions')))
            for i in range(0,len(self.report['plan']),5):
                self.open_group(self.report['plan'][i:i+5]);self.sample()
            self.report['allSymbolsOpenedAt']=int(time.time()*1000);self.save()
            self.partial_probes()
            # Ten minutes of monitoring AFTER all 50 probes have been admitted.
            deadline=time.monotonic()+600;minute=-1
            while time.monotonic()<deadline:
                tick=time.monotonic();self.sample()
                m=int((self.report['snapshots'][-1]['at']-self.report['allSymbolsOpenedAt'])/60000)
                if m!=minute:
                    minute=m
                    try:
                        with ThreadPoolExecutor(max_workers=3) as pool:
                            for sym,rows in pool.map(self.candles,self.report['plan']):
                                self.monitor_replay(deadline)
                                self.book.bars[sym]=[[float(r[k]) for k in ('open','high','low','close','volume')] for r in rows]
                                self.book.bar_t[sym]=int(rows[-1]['time'])/1000
                        self.book.replay_all(now=max(self.book.bar_t.values())+60,on_step=lambda: self.monitor_replay(deadline))
                    except ObservationDeadline:
                        # A partially refreshed book is not a completed native
                        # decision sample. Continue directly to owned cleanup.
                        self.report['shadowDeadlineReached']=True
                        break
                    self.shadow()
                time.sleep(max(0,min(10-(time.monotonic()-tick),deadline-time.monotonic())))
            self.report['activeEndedAt']=int(time.time()*1000)
        except Exception as exc:
            self.report['error']=str(exc);self.save()
            raise
        finally:
            failures=self.cleanup_all()
            self.report['cleanupErrors']=failures;self.save()
            if failures:raise RuntimeError('Some owned demo positions could not be reconciled; see cleanupErrors')
            positions=self.api.rows('/openApi/swap/v2/user/positions')
            orders=self.api.rows('/openApi/swap/v2/trade/openOrders',key='orders')
            own={(c['symbol'],c['side']) for c in self.report['cases']}
            ids={str(r['body'].get('clientOrderID','')).lower() for r in self.log if r['method']=='POST'}
            self.check('final-owned-exposure-flat',not any((p['symbol'],p['positionSide']) in own
                and abs(float(p.get('positionAmt') or 0))>0 for p in positions))
            self.check('final-owned-orders-empty',not any(str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower() in ids for o in orders))
        self.finish_stats();self.report.update(complete=True,executed=True);self.save()


def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--credentials',required=True,type=Path)
    a.add_argument('--out',required=True,type=Path);a.add_argument('--execute',action='store_true');args=a.parse_args()
    if args.out.exists():a.error('Use a new journal directory')
    s=Session(Client(args.credentials),args.out)
    try:s.run(args.execute)
    except Exception as exc:s.report['error']=str(exc);s.save();print(json.dumps({'error':str(exc)}));return 1
    return 0
if __name__=='__main__':raise SystemExit(main())
