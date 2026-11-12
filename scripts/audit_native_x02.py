#!/usr/bin/env python3
"""Read-only, exact-order reconciliation of a completed native VST session."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

from x02_demo_acceptance import ROOT
from x02_native_session import HOST, CONN, order_bodies, atomic_json
sys.path.insert(0,str(ROOT/'server/pulse'))
from execution_cost import execution_result
from bingx_fast import FastBingX,ErrorLog


def dec(value):
    number=Decimal(str(value))
    if not number.is_finite():raise ValueError('nonfinite venue quantity or value')
    return number


def cash_group(rows):
    """Independent Decimal cash ledger; prices already contain execution slip."""
    entry_qty=exit_qty=entry_value=exit_value=commission=Decimal(0)
    for row in rows:
        q,p=dec(row['volume']),dec(row['price'])
        if q<0 or p<=0:raise ValueError('invalid actual fill')
        if str(row.get('currency','USDT')).upper() not in ('USDT','VST'):
            raise ValueError('unsupported commission settlement currency')
        opening=(row['positionSide'],row['side'])in(('LONG','BUY'),('SHORT','SELL'))
        if opening:entry_qty+=q;entry_value+=q*p
        else:exit_qty+=q;exit_value+=q*p
        commission+=dec(row['commission'])
    if entry_qty!=exit_qty or entry_qty<=0:raise ValueError('actual own fill ledger is not flat')
    side=rows[0]['positionSide']
    gross=(exit_value-entry_value)*(1 if side=='LONG' else -1)
    net=gross+commission
    return dict(quantity=float(entry_qty),entryValue=float(entry_value),exitValue=float(exit_value),
                gross=float(gross),commission=float(commission),net=float(net))


def position_cash(entry, rows):
    """Independent precise cash proof for one exclusively owned venue position.

    Order/fill commissions have six-place display rounding on VST. Position
    history supplies the full commission. External exits are evidence of cash,
    never evidence that their order identities belong to this session.
    """
    pid=str(entry.get('positionID')or entry.get('positionId')or'')
    matches=[r for r in rows if str(r.get('positionId')or r.get('positionID'))==pid]
    if not pid.isdigit()or len(matches)!=1:raise ValueError('missing exact position history')
    row=matches[0];side=entry['positionSide']
    if (row.get('symbol')!=entry['symbol']or row.get('positionSide')!=side
            or str(row.get('closeAllPositions')).lower()!='true'):
        raise ValueError('position history identity or terminal state mismatch')
    quantity=dec(entry['executedQty']);price=dec(entry['avgPrice'])
    if quantity<=0 or price<=0:raise ValueError('invalid owned entry')
    if any(dec(row[k])!=quantity for k in('positionAmt','closePositionAmt')):
        raise ValueError('position history contains different or mixed inventory')
    if dec(row['avgPrice'])!=price:raise ValueError('position entry price mismatch')
    exit_price=dec(row['avgClosePrice']);commission=dec(row['positionCommission'])
    if exit_price<=0 or commission>0:raise ValueError('invalid price or unsupported rebate')
    gross=quantity*(exit_price-price)*(1 if side=='LONG'else -1)
    if abs(gross-dec(row['realisedProfit']))>Decimal('.0001000001'):
        raise ValueError('position gross differs from venue rounded cash')
    opened,ended=dec(row['openTime'])/1000,dec(row['updateTime'])/1000
    if ended<opened or abs(opened-dec(entry['time'])/1000)>60:
        raise ValueError('position history time mismatch')
    return dict(symbol=entry['symbol'],side=side,positionId=pid,
                client_id=str(entry.get('clientOrderId')or entry.get('clientOrderID')).lower(),
                quantity=float(quantity),entry=float(price),exit=float(exit_price),
                entryValue=float(quantity*price),exitValue=float(quantity*exit_price),
                gross=float(gross),commission=float(commission),net=float(gross+commission),
                funding=float(dec(row.get('totalFunding')or 0)),closedAt=float(ended))


def audit(run,credentials):
    report=json.loads((run/'session.json').read_text())
    if report.get('host')!=HOST or report.get('connection')!=CONN or not report.get('activeEndedAt'):
        raise ValueError('native VST observation has not ended')
    events=[json.loads(line)for line in(run/'mutation-journal.jsonl').read_text().splitlines()if line]
    intents={row['index']:dict(row)for row in events if row.get('result')=='pending'}
    for row in events:
        if row.get('result')=='response'and row.get('index')in intents:
            intents[row['index']]['response']=row['response']
    tag=report['tag'].lower();symbols=set(report['symbols']);requested={};refused=[]
    for intent in intents.values():
        if intent['method']!='POST':continue
        bodies=order_bodies(intent['path'],intent['body'])
        for body in bodies:
            cid=str(body.get('clientOrderID')or body.get('clientOrderId')or'').lower()
            if body.get('symbol')not in symbols or not cid.startswith(tag):
                raise ValueError('native intent escaped its exact ownership scope')
        reply=intent.get('response',{});code=str(reply.get('code',''))
        if code not in ('0','','-1')and not reply.get('error'):
            refused.append(dict(index=intent['index'],code=code,message=str(reply.get('msg',''))[:200]))
            continue
        data=reply.get('data')or{}
        acknowledged=data.get('orders',[])if isinstance(data,dict)else data
        if isinstance(data,dict)and isinstance(data.get('order'),dict):acknowledged=[data['order']]
        if not isinstance(acknowledged,list):acknowledged=[]
        for body in bodies:
            cid=str(body.get('clientOrderID')or body.get('clientOrderId')or'').lower()
            key=(body['symbol'],cid)
            if key in requested:raise ValueError('duplicate submitted client identity')
            requested[key]=dict(body)
            for ack in acknowledged:
                found=str(ack.get('clientOrderID')or ack.get('clientOrderId')or'').lower()
                if found==cid and ack.get('symbol')==body['symbol']and ack.get('orderId'):
                    requested[key]['_venueOrderId']=str(ack['orderId'])
    if credentials.suffix=='.json':
        values=json.loads(credentials.read_text());key,secret=values['api_key'],values['api_secret']
        if values.get('base_url')!=HOST:raise ValueError('audit credentials are not explicitly VST')
    else:
        lines=[row.strip()for row in credentials.read_text().splitlines()if row.strip()]
        key,secret=lines[lines.index('API Key')+1],lines[lines.index('Secret Key')+1]
    native=FastBingX(key,secret,ErrorLog(str(run/'audit-read-errors.jsonl')),base=HOST)
    def call(method,path,params):
        if method!='GET':raise ValueError('fill audit is read-only')
        for attempt in range(3):
            reply=native.get(path,params)
            if not reply.get('error'):return reply
            if attempt<2:time.sleep(.5)
        raise ValueError('venue read unavailable: '+path)
    class ReadOnly:
        pass
    api=ReadOnly();api.call=call
    end_ms=int(time.time()*1000)
    start_ms=int(report['createdAt']*1000)-60000

    def read_order(item):
        (symbol,cid),body=item
        identity={'orderId':body['_venueOrderId']}if body.get('_venueOrderId')else{'clientOrderId':cid}
        reply=api.call('GET','/openApi/swap/v2/trade/order',{'symbol':symbol,**identity})
        if (not body.get('_venueOrderId') and body['type']=='MARKET'
                and time.time()-report['activeEndedAt']>20):
            misses=0
            for _ in range(3):
                if str(reply.get('code'))=='109421'and'order not exist'in str(reply.get('msg','')).lower():misses+=1
                else:break
                reply=api.call('GET','/openApi/swap/v2/trade/order',{'symbol':symbol,**identity})
            if misses==3:return body,{'status':'NOT_ACCEPTED','orderId':'missing:'+cid},[],True
        data=reply.get('data')or{};order=data.get('order',data)
        found=str(order.get('clientOrderID')or order.get('clientOrderId')or'').lower()
        exact=found==cid or (body.get('_venueOrderId')and str(order.get('orderId'))==body['_venueOrderId'])
        if (str(reply.get('code'))!='0'or not exact or order.get('symbol')!=symbol
                or order.get('positionSide')!=body.get('positionSide')or order.get('side')!=body.get('side')):
            raise ValueError('own order query did not return the exact submitted identity')
        qty=dec(order.get('executedQty',0));fills=[]
        if qty:
            response=api.call('GET','/openApi/swap/v2/trade/allFillOrders',
                             {'tradingUnit':'COIN','startTs':start_ms,'endTs':end_ms,'orderId':str(order['orderId'])})
            fills=(response.get('data')or{}).get('fill_orders')or[]
            if str(response.get('code'))!='0'or not fills:
                raise ValueError('filled own order has no actual fill evidence')
            for row in fills:
                if (str(row.get('orderId'))!=str(order['orderId'])or row.get('symbol')!=symbol
                        or row.get('positionSide')!=body.get('positionSide')or row.get('side')!=body.get('side')):
                    raise ValueError('fill query returned a different order or direction')
            if sum((dec(row['volume'])for row in fills),Decimal(0))!=qty:
                raise ValueError('complete per-order fill sum differs from cumulative executed quantity')
        terminal=order.get('status')in('FILLED','CANCELED','CANCELLED','REJECTED','EXPIRED')
        return body,order,fills,terminal

    cache_path=run/'native-order-receipts.json'
    saved=json.loads(cache_path.read_text())if cache_path.exists()else[]
    cached={(r['request']['symbol'],str(r['request'].get('clientOrderID')or r['request'].get('clientOrderId')).lower()):r for r in saved}
    if cached and (set(cached)!=set(requested)or len(cached)!=len(saved)):
        raise ValueError('receipt cache does not match this exact immutable journal')
    receipts=[];pending=[]
    for identity,body in requested.items():
        row=cached.get(identity)
        if row and row['terminal'] and row['request']==body:
            order=row['order'];cid=str(order.get('clientOrderId')or order.get('clientOrderID')or'').lower()
            if order.get('status')!='NOT_ACCEPTED'and(cid!=identity[1]or order.get('symbol')!=identity[0]):
                raise ValueError('terminal receipt identity differs from its journal')
            receipts.append((body,order,row['fills'],True))
        else:pending.append((identity,body))
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='own-fill-audit')as pool:
        receipts.extend(pool.map(read_order,pending))
    atomic_json(run/'native-order-receipts.json',[
        dict(request=body,order=order,fills=fills,terminal=terminal)for body,order,fills,terminal in receipts])
    all_fills=[];orders=[];entries={};owned_exit_orders=[];unterminated=[]
    for body,order,fills,terminal in receipts:
        oid=str(order['orderId']);symbol=body['symbol'];side=body['positionSide']
        if not terminal:unterminated.append(hashlib.sha256(oid.encode()).hexdigest()[:16])
        if fills:
            all_fills.extend(fills)
            pid=str(order.get('positionID')or order.get('positionId')or'')
            opening=body['side']==('BUY'if side=='LONG'else'SELL')
            if opening:
                if not pid.isdigit()or pid in entries:raise ValueError('audit requires a unique exact owned entry per position')
                entries[pid]=order
            else:owned_exit_orders.append(order)
        qty=sum((dec(r['volume'])for r in fills),Decimal(0))
        value=sum((dec(r['volume'])*dec(r['price'])for r in fills),Decimal(0))
        commission=sum((dec(r['commission'])for r in fills),Decimal(0))
        orders.append(dict(symbol=symbol,positionSide=side,side=body['side'],type=body['type'],
                      status=order.get('status'),qty=float(qty),value=float(value),commission=float(commission),
                      fills=len(fills),orderHash=hashlib.sha256(oid.encode()).hexdigest()[:16]))
    comparisons=[];histories={}
    history_path=run/'native-position-history.json'
    old_histories=json.loads(history_path.read_text())if history_path.exists()else{}
    for pid,entry in entries.items():
        response=old_histories.get(pid)
        if response is None:
            time.sleep(.25) # documented history endpoint: 5/s, no startup burst
            response=api.call('GET','/openApi/swap/v1/trade/positionHistory',
                {'symbol':entry['symbol'],'positionId':pid,'startTs':start_ms,'endTs':end_ms,'pageSize':100})
        if str(response.get('code'))!='0':raise ValueError('position history unavailable')
        histories[pid]=response
        data=response.get('data')or{}
        rows=data.get('positionHistory',data.get('list',[]))if isinstance(data,dict)else data
        group=position_cash(entry,rows)
        result=execution_result(group['entry'],group['exit'],group['quantity'],group['side'],0,-group['commission'])
        if abs(result['net']-group['net'])>1e-10:raise ValueError('engine arithmetic differs from independent actual position cash')
        group['engineRecomputedNet']=result['net'];comparisons.append(group)
        atomic_json(history_path,histories)
    owned_exits={}
    for order in owned_exit_orders:
        pid=str(order.get('positionID')or order.get('positionId')or'')
        # BingX triggered children report positionID=0. This is already an
        # exact journal-owned order; allocate it only to a unique owned
        # position whose verified lifetime contains its actual fill time.
        if pid not in entries:
            when=float(order['updateTime'])/1000
            matches=[g['positionId']for g in comparisons if g['symbol']==order['symbol']and g['side']==order['positionSide']
                     and float(entries[g['positionId']]['time'])/1000<=when<=g['closedAt']]
            if pid not in('','0')or len(matches)!=1:raise ValueError('owned triggered exit cannot be allocated uniquely')
            pid=matches[0]
        owned_exits[pid]=owned_exits.get(pid,Decimal(0))+dec(order['executedQty'])
    for group in comparisons:
        own_exit=owned_exits.get(group['positionId'],Decimal(0));quantity=dec(entries[group['positionId']]['executedQty'])
        if own_exit>quantity:raise ValueError('owned exit exceeds exact position inventory')
        group['externalOrUnjournaledClose']=own_exit<quantity
    # Stats retain only their most recent closes. Union immutable close receipts
    # across every capture, so earlier losses cannot disappear at the row cap.
    closed={}
    sources=[json.loads(path.read_text()).get('closed',[])for path in sorted(run.glob('*stats.json'))]
    durable=run/'state'/('trades-'+CONN+'.jsonl')
    if durable.exists():sources.append([json.loads(line)for line in durable.read_text().splitlines()if line])
    for records in sources:
        for row in records:
            if row.get('ours')is not True:continue
            cid=str(row.get('client_id','')).lower()
            if row.get('symbol')not in symbols or not cid.startswith(tag):
                raise ValueError('closed record escaped native ownership')
            key=(cid,row['symbol'],row.get('t'),row.get('qty'))
            closed[key]=row
    verified=[r for r in closed.values()if 'unverified'not in r.get('reason','')and not r.get('exit_price_estimated')]
    by_client={r['client_id'].lower():r for r in verified}
    if len(by_client)!=len(verified):raise ValueError('duplicate or partial parent settlements need explicit allocation')
    financial=[]
    for group in comparisons:
        row=by_client.get(group['client_id'])
        original=next((r for r in closed.values()if r['client_id'].lower()==group['client_id']),{})
        if row and abs(float(row['qty'])-group['quantity'])>1e-10:raise ValueError('stored close quantity differs from position ledger')
        financial.append(dict(original,client_id=group['client_id'],symbol=group['symbol'],side=group['side'],
            qty=group['quantity'],entry=group['entry'],exit=group['exit'],t=group['closedAt'],pnl=group['net'],
            pnl_pct=group['gross']/group['entryValue'],net_pnl_pct=group['net']/group['entryValue'],
            fees_usdt=-group['commission'],fee_source='venue-position-history',
            reason=('external-position-close'if group['externalOrUnjournaledClose']else original.get('reason','unclassified')),
            original_engine_reason=original.get('reason'),engine_record_verified=bool(row)))
    stored=sum(float(row['pnl'])for row in verified)
    measured=sum(row['net']for row in comparisons)
    difference=stored-measured
    matched_difference=stored-sum(row['net']for row in comparisons if row['client_id']in by_client)
    engine_complete=len(verified)==len(comparisons)and all(r['client_id']in by_client for r in comparisons)
    if set(by_client)-{r['client_id']for r in comparisons}:raise ValueError('stored close has no exact owned entry')
    positions=api.call('GET','/openApi/swap/v2/user/positions',{})
    open_orders=api.call('GET','/openApi/swap/v2/trade/openOrders',{})
    if str(positions.get('code'))!='0'or str(open_orders.get('code'))!='0':raise ValueError('final scope read unavailable')
    active_positions=[r for r in positions['data']if r['symbol']in symbols and abs(float(r['positionAmt']))>1e-12]
    active_orders=[r for r in (open_orders['data'].get('orders')or[])if r['symbol']in symbols]
    verified_flat=not active_positions and not active_orders and not unterminated
    result=dict(schema='cts-g-native-fill-audit-v2',environment='BingX VST x02',
                passed=verified_flat and engine_complete and abs(difference)<1e-8,
                ledgerPassed=verified_flat,engineComplete=engine_complete,verifiedFlat=verified_flat,
                method='exact own order/fill receipts and exclusively owned position history; independent Decimal cash; full-precision position commissions once; no extra slippage debit',
                orders=orders,comparisons=comparisons,financialCloses=financial,closedRecords=len(verified),
                estimatedOrUnverifiedRecords=len(closed)-len(verified),actualClosedPositions=len(comparisons),
                externalOrUnjournaledCloses=sum(r['externalOrUnjournaledClose']for r in comparisons),
                actualFills=len(all_fills),actualFilledOrders=sum(bool(row['fills'])for row in orders),
                storedEngineNet=stored,actualFillNet=measured,storedNetDifference=difference,
                matchedEngineNetDifference=matched_difference,
                nonterminalOwnOrders=unterminated,definiteRefusals=refused,
                accountBalanceUsedForAttribution=False,cleanup=report['cleanup'])
    atomic_json(run/'native-fill-details.json',all_fills)
    atomic_json(run/'native-fill-audit.json',result)
    native.http.close()
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run',type=Path,required=True);ap.add_argument('--credentials',type=Path,required=True)
    args=ap.parse_args();result=audit(args.run,args.credentials)
    print(json.dumps({key:result[key]for key in('passed','actualFills','actualFilledOrders','closedRecords','actualFillNet','storedNetDifference')}),flush=True)
    if not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
