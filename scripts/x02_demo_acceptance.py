#!/usr/bin/env python3
"""Bounded x02 VST order acceptance check. Defaults to a read-only preflight.

An explicit --execute performs two small stacked round trips on empty symbols.
The host is fixed to virtual VST. No account-wide cancellation, leverage change,
mode change or mainnet URL is supported. A durable journal precedes each write.
Credentials are read locally and never copied into the journal or report.
"""
from __future__ import annotations
import argparse
from decimal import Decimal, ROUND_CEILING
import hashlib
import hmac
import json
import math
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'server/pulse'))
from pulse_trader import ctrl_payload

HOST='https://open-api-vst.bingx.com'
ORDER='/openApi/swap/v2/trade/order'


class Client:
    def __init__(self, credentials):
        lines=[x.strip() for x in credentials.read_text().splitlines() if x.strip()]
        self.key=lines[lines.index('API Key')+1]
        self.secret=lines[lines.index('Secret Key')+1]
        self.offset=0
        now=self.call('GET','/openApi/swap/v2/server/time',signed=False)['data']['serverTime']
        self.offset=int(now)-int(time.time()*1000)

    def call(self,method,path,params=None,*,signed=True):
        p=dict(params or {})
        if signed:p.update(timestamp=int(time.time()*1000)+self.offset,recvWindow=10000)
        raw='&'.join(f'{k}={p[k]}' for k in sorted(p))
        qs=urllib.parse.urlencode(sorted(p.items()))
        if signed:qs+='&signature='+hmac.new(self.secret.encode(),raw.encode(),hashlib.sha256).hexdigest()
        req=urllib.request.Request(HOST+path+('?' + qs if qs else ''),method=method,
            headers={'X-BX-APIKEY':self.key} if signed else {})
        try:
            with urllib.request.urlopen(req,timeout=10) as response:data=json.load(response)
        except Exception as exc:
            # Transport exceptions may contain a signed URL. Never serialize it.
            raise RuntimeError('VST transport failure: '+type(exc).__name__) from None
        if not isinstance(data,dict):raise RuntimeError('VST invalid response shape')
        if data.get('code')!=0:
            msg=str(data.get('msg','')).replace(self.key,'[redacted]').replace(self.secret,'[redacted]')
            raise RuntimeError(f'VST API {data.get("code")}: {msg[:220]}')
        return data

    def rows(self,path,params=None,key=None):
        data=self.call('GET',path,params)['data']
        if key and isinstance(data,dict):data=data.get(key)
        if not isinstance(data,list):raise RuntimeError('VST list response unknown')
        return data

    def position(self,symbol,side):
        rows=self.rows('/openApi/swap/v2/user/positions',{'symbol':symbol})
        matching=[r for r in rows if r.get('symbol')==symbol and r.get('positionSide')==side]
        qty=sum(abs(float(r['positionAmt'])) for r in matching)
        if not math.isfinite(qty):raise RuntimeError('VST non-finite position')
        return qty,matching

    def order(self,symbol,cid,oid=None,*,pending_control=False):
        if pending_control:
            rows=self.rows('/openApi/swap/v2/trade/openOrders',{'symbol':symbol},'orders')
            found=[r for r in rows if (oid and str(r.get('orderId'))==str(oid)) or
                   str(r.get('clientOrderID') or r.get('clientOrderId') or '').lower()==cid.lower()]
            if len(found)!=1:raise RuntimeError('VST pending control unconfirmed')
            return found[0]
        params={'symbol':symbol,'orderId':oid} if oid else {'symbol':symbol,'clientOrderID':cid.lower()}
        data=self.call('GET',ORDER,params)['data']
        if isinstance(data,dict) and isinstance(data.get('order'),dict):data=data['order']
        if not isinstance(data,dict) or not data.get('orderId'):raise RuntimeError('VST order unconfirmed')
        return data


class Acceptance:
    def __init__(self,client,out):
        self.api=client;self.out=out;self.prefix='Gx02qa'+uuid.uuid4().hex[:12]
        self.seq=0;self.log=[];self.report={'environment':'bingx-x02 VST demo','host':HOST,'runId':self.prefix,
            'startedUtcMs':int(time.time()*1000),'checks':[],'cases':[],'complete':False}

    def save(self):
        self.out.mkdir(parents=True,exist_ok=True)
        for name,data in [('journal.json',self.log),('report.json',self.report)]:
            path=self.out/name;tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,indent=2,allow_nan=False));tmp.replace(path)

    def check(self,name,ok,**details):
        self.report['checks'].append(dict(name=name,ok=bool(ok),**details));self.save()
        print(json.dumps(self.report['checks'][-1]),flush=True)
        if not ok:raise RuntimeError('Acceptance failed: '+name)

    def write(self,method,path,body):
        intent={'method':method,'path':path,'body':dict(body),'t':int(time.time()*1000),'result':'pending'}
        self.log.append(intent);self.save()
        try:r=self.api.call(method,path,body)
        except Exception as exc:
            intent['result']='unconfirmed-or-rejected';intent['error']=str(exc);self.save();raise
        intent['result']='accepted';intent['response']=r;self.save();return r

    def cid(self):
        self.seq+=1;return self.prefix+str(self.seq)

    def market(self,symbol,position_side,side,qty):
        cid=self.cid();body=dict(symbol=symbol,positionSide=position_side,side=side,type='MARKET',quantity=str(qty),clientOrderID=cid)
        try:self.write('POST',ORDER,body)
        except RuntimeError:
            # Query the original identity after an uncertain response; never
            # retry a market order under a new ID while its result is unknown.
            order=self.api.order(symbol,cid)
        else:order=self.api.order(symbol,cid)
        filled=float(order.get('executedQty') or 0);price=float(order.get('avgPrice') or 0)
        self.check('market-fill',filled>0 and price>0 and order.get('status')=='FILLED',symbol=symbol,
            positionSide=position_side,side=side,requestedQty=float(qty),filledQty=filled,price=price,orderId=str(order['orderId']))
        return order

    def clear(self,symbol):
        rows=self.api.rows('/openApi/swap/v2/user/positions',{'symbol':symbol})
        orders=self.api.rows('/openApi/swap/v2/trade/openOrders',{'symbol':symbol},'orders')
        self.check('symbol-flat-preflight',all(abs(float(r.get('positionAmt',0)))<=1e-12 for r in rows) and not orders,symbol=symbol)

    def controls(self,symbol,side,entry,qty,precision,case):
        close_side='SELL' if side=='LONG' else 'BUY';sd=1 if side=='LONG' else -1
        for kind,factor in [('sl',1-sd*.05),('tp',1+sd*.08)]:
            px=f'{entry*factor:.{precision}f}';cid=self.cid()
            body=ctrl_payload(symbol,side,kind,px,str(qty),cid,close_pos=True)
            try:response=self.write('POST',ORDER,body)
            except RuntimeError as exc:
                # A documented missing-quantity rejection is safe to retry with
                # the same ID and an explicit quantity. Other errors stop QA.
                if 'quantity' not in str(exc).lower():raise
                body['quantity']=str(qty);response=self.write('POST',ORDER,body)
            accepted=response.get('data',{});accepted=accepted.get('order',accepted)
            oid=str(accepted.get('orderId') or accepted.get('orderID') or '')
            case['controls'].append({'cid':cid,'orderId':oid,'kind':kind,'requested':body})
            self.save()
            order=self.api.order(symbol,cid,oid,pending_control=True)
            self.check('overall-control',str(order.get('closePosition')).lower() in ('true','1') and
                order.get('positionSide')==side and abs(float(order.get('stopPrice',0))-float(px))<10**-precision,
                symbol=symbol,side=side,kind=kind,orderId=str(order['orderId']),stopPrice=float(order.get('stopPrice',0)),
                closePosition=order.get('closePosition'),reportedQuantity=order.get('origQty',order.get('quantity')))

    def cleanup(self,case):
        symbol,side=case['symbol'],case['side']
        # Resolve every submitted entry/close before treating a flat REST read
        # as final. An order with a lost response can still fill afterwards.
        submitted=[r for r in self.log if r['method']=='POST' and r['body'].get('symbol')==symbol
                   and r['body'].get('positionSide')==side and r['body'].get('type')=='MARKET']
        for intent in submitted:
            order=self.api.order(symbol,intent['body']['clientOrderID'])
            if order.get('status') not in ('FILLED','CANCELED','CANCELLED','EXPIRED','REJECTED'):
                self.write('DELETE',ORDER,{'symbol':symbol,'orderId':str(order['orderId'])})
                order=self.api.order(symbol,intent['body']['clientOrderID'])
                if order.get('status') not in ('FILLED','CANCELED','CANCELLED','EXPIRED','REJECTED'):
                    raise RuntimeError('Unresolved test market order; protective orders retained')
        # The empty preflight plus this run's journal establishes the test
        # exposure. Reject a concurrently enlarged position rather than close
        # another writer's amount. Keep controls while exposure is uncertain.
        qty,_=self.api.position(symbol,side)
        if qty>case['maxQty']+1e-9:raise RuntimeError('Concurrent exposure: own controls retained')
        if qty:self.market(symbol,side,'SELL' if side=='LONG' else 'BUY',qty)
        left,_=self.api.position(symbol,side);self.check('flat-after-close',left<=1e-12,symbol=symbol,side=side,remaining=left)
        orders=self.api.rows('/openApi/swap/v2/trade/openOrders',{'symbol':symbol},'orders')
        own={x['cid'].lower() for x in case['controls']}
        # Include a submitted control whose response was lost, by journal ID.
        own.update(str(x['body'].get('clientOrderID','')).lower() for x in self.log if x['method']=='POST' and
            x['body'].get('symbol')==symbol and x['body'].get('positionSide')==side and x['body'].get('type') in ('STOP_MARKET','TAKE_PROFIT_MARKET'))
        for order in orders:
            cid=str(order.get('clientOrderID') or order.get('clientOrderId') or '').lower()
            if cid in own and order.get('symbol')==symbol and order.get('positionSide')==side:
                self.write('DELETE',ORDER,{'symbol':symbol,'orderId':str(order['orderId'])})
        remaining=self.api.rows('/openApi/swap/v2/trade/openOrders',{'symbol':symbol},'orders')
        self.check('no-stranded-test-orders',not any(str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower() in own for o in remaining),symbol=symbol,side=side)
        case['cleaned']=True;self.save()

    def run_case(self,symbol,side,contract):
        self.clear(symbol)
        data=self.api.call('GET','/openApi/swap/v2/quote/ticker',{'symbol':symbol},signed=False)['data']
        row=data[0] if isinstance(data,list) else data;price=float(row['lastPrice'])
        step=Decimal(10)**-int(contract['quantityPrecision'])
        need=max(Decimal(str(contract['tradeMinQuantity'])),Decimal(str(contract['tradeMinUSDT']))/Decimal(str(price)))
        qty=need.quantize(step,rounding=ROUND_CEILING)
        self.check('bounded-demo-notional',float(qty)*price*3<=25,symbol=symbol,maximumNotional=float(qty)*price*3)
        case={'symbol':symbol,'side':side,'maxQty':float(qty*3),'controls':[],'fills':[],'cleaned':False}
        self.report['cases'].append(case);self.save()
        try:
            order=self.market(symbol,side,'BUY' if side=='LONG' else 'SELL',qty*2);case['fills'].append(order);self.save()
            self.controls(symbol,side,float(order['avgPrice']),qty*2,int(contract['pricePrecision']),case)
            order=self.market(symbol,side,'BUY' if side=='LONG' else 'SELL',qty);case['fills'].append(order);self.save()
            total,_=self.api.position(symbol,side);self.check('stack-total',abs(total-float(qty*3))<1e-9,symbol=symbol,side=side,qty=total)
            for ctrl in case['controls']:
                control=self.api.order(symbol,ctrl['cid'],ctrl['orderId'],pending_control=True)
                self.check('overall-covers-growing-position',str(control.get('closePosition')).lower() in ('true','1') and control.get('status')=='NEW',symbol=symbol,side=side,orderId=ctrl['orderId'])
            order=self.market(symbol,side,'SELL' if side=='LONG' else 'BUY',qty);case['fills'].append(order);self.save()
            remaining,_=self.api.position(symbol,side)
            self.check('partial-remainder',abs(remaining-float(qty*2))<1e-9,symbol=symbol,side=side,remaining=remaining)
        finally:self.cleanup(case)


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--credentials',type=Path,required=True)
    ap.add_argument('--out',type=Path,required=True);ap.add_argument('--execute',action='store_true');args=ap.parse_args()
    if args.out.exists():ap.error('preserve the existing journal; choose a new run directory')
    run=Acceptance(Client(args.credentials),args.out);run.save()
    try:
        positions=run.api.rows('/openApi/swap/v2/user/positions');orders=run.api.rows('/openApi/swap/v2/trade/openOrders',key='orders')
        run.check('authenticated-demo-read',True,positionRows=len(positions),openOrderRows=len(orders))
        if args.execute:
            balances=run.api.rows('/openApi/swap/v3/user/balance')
            run.check('virtual-currency',any(r.get('asset')=='VST' for r in balances))
            contracts=run.api.call('GET','/openApi/swap/v2/quote/contracts',signed=False)['data']
            for symbol,side in [('DOGE-USDT','LONG'),('ADA-USDT','SHORT')]:
                contract=next(r for r in contracts if r['symbol']==symbol)
                run.run_case(symbol,side,contract)
        run.report['executed']=args.execute;run.report['complete']=True;run.save();return 0
    except Exception as exc:
        run.report['error']=str(exc);run.save();print(json.dumps({'complete':False,'error':str(exc)}));return 1


if __name__=='__main__':raise SystemExit(main())
