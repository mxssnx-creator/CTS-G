"""Read-only reconciliation of the bounded acceptance run against actual VST fills."""
from decimal import Decimal
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from x02_demo_acceptance import Client,ROOT
sys.path.insert(0,str(ROOT/'server/pulse'))
from execution_cost import execution_result


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--credentials',type=Path,required=True)
    ap.add_argument('--run',type=Path,required=True);args=ap.parse_args()
    report=json.loads((args.run/'report.json').read_text());journal=json.loads((args.run/'journal.json').read_text())
    if not report.get('complete') or not all(c['cleaned'] for c in report['cases']):raise ValueError('acceptance not complete/flat')
    api=Client(args.credentials);orders=[];groups={};raw=[]
    for item in journal:
        if item['method']!='POST' or item['body'].get('type')!='MARKET' or item['result']!='accepted':continue
        ack=item['response']['data']['order'];oid=str(ack['orderId']);body=item['body']
        response=api.call('GET','/openApi/swap/v2/trade/allFillOrders',{'tradingUnit':'COIN',
            'startTs':report['startedUtcMs']-60000,'endTs':int(time.time()*1000),'orderId':oid})
        rows=response['data']['fill_orders']
        if not rows or any(str(r['orderId'])!=oid or r['symbol']!=body['symbol'] or r['positionSide']!=body['positionSide'] for r in rows):raise ValueError('unexpected/missing trade fills')
        qty=sum(Decimal(r['volume']) for r in rows);value=sum(Decimal(r['volume'])*Decimal(r['price']) for r in rows)
        commission=sum(Decimal(r['commission']) for r in rows)
        if qty!=Decimal(ack['executedQty']):raise ValueError('order/fill quantity mismatch')
        entry=body['side']==('BUY' if body['positionSide']=='LONG' else 'SELL')
        group=groups.setdefault((body['symbol'],body['positionSide']),dict(entryQty=Decimal(0),exitQty=Decimal(0),entryValue=Decimal(0),exitValue=Decimal(0),entryCommission=Decimal(0),exitCommission=Decimal(0)))
        prefix='entry' if entry else 'exit';group[prefix+'Qty']+=qty;group[prefix+'Value']+=value;group[prefix+'Commission']+=commission
        raw.extend(rows)
        orders.append(dict(symbol=body['symbol'],positionSide=body['positionSide'],side=body['side'],qty=float(qty),value=float(value),
            commission=float(commission),fills=len(rows),orderHash=hashlib.sha256(oid.encode()).hexdigest()[:16]))
    comparisons=[]
    for (symbol,side),g in groups.items():
        if g['entryQty']!=g['exitQty']:raise ValueError('acceptance book is not flat in fill ledger')
        gross=(g['exitValue']-g['entryValue'])*(1 if side=='LONG' else -1)
        net=gross+g['entryCommission']+g['exitCommission']
        result=execution_result(float(g['entryValue']/g['entryQty']),float(g['exitValue']/g['exitQty']),float(g['entryQty']),side,
            -float(g['entryCommission']),-float(g['exitCommission']))
        if abs(result['net']-float(net))>1e-10:raise ValueError('engine net disagrees with independent fill ledger')
        remaining,_=api.position(symbol,side)
        orders_now=api.rows('/openApi/swap/v2/trade/openOrders',{'symbol':symbol},'orders')
        own=[o for o in orders_now if str(o.get('clientOrderID') or o.get('clientOrderId') or '').lower().startswith(report['runId'].lower())]
        if remaining or own:raise ValueError('acceptance left exposure or test orders')
        comparisons.append(dict(symbol=symbol,side=side,quantity=float(g['entryQty']),gross=float(gross),
            fees=-float(g['entryCommission']+g['exitCommission']),net=float(net),engineNet=result['net'],remaining=remaining,ownOrders=len(own)))
    (args.run/'fill-details.json').write_text(json.dumps(raw,indent=2))
    audit={'passed':True,'environment':'x02 VST demo','method':'Decimal cash ledger vs engine execution_result; actual reported commissions once',
        'orders':orders,'comparisons':comparisons,'scope':'acceptance round trips; not strategy profitability or a live engine qualification'}
    (args.run/'fee-audit.json').write_text(json.dumps(audit,indent=2));print(json.dumps(audit),flush=True)


if __name__=='__main__':main()
