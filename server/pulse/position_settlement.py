"""Verify a closed, exclusively owned position by its exact venue position ID.

This consumes read-only evidence. An external closing order is never adopted
as an order owned by the engine, or authorized for mutation.
"""
import math


def verify_position_settlement(pos,entry,rows,now):
    if (not getattr(pos,'ours',False) or not getattr(pos,'order_id','')
            or getattr(pos,'block_fills',[])or getattr(pos,'dca_fills',[])
            or getattr(pos,'close_filled_qty',0)>0):return None
    if (str(entry.get('orderId'))!=str(pos.order_id)or entry.get('symbol')!=pos.symbol
            or entry.get('positionSide')!=pos.side
            or entry.get('side')!=('BUY'if pos.side=='LONG'else'SELL')):return None
    cid=str(entry.get('clientOrderId')or entry.get('clientOrderID')or'')
    if not cid or cid.lower()!=str(pos.client_id).lower():return None
    pid=str(entry.get('positionID')or entry.get('positionId')or'')
    if not pid.isdigit()or int(pid)<=0:return None
    if getattr(pos,'position_id','')and str(pos.position_id)!=pid:return None
    matches=[r for r in rows if str(r.get('positionId')or r.get('positionID'))==pid]
    if len(matches)!=1:return None
    r=matches[0]
    if (r.get('symbol')!=pos.symbol or r.get('positionSide')!=pos.side
            or str(r.get('closeAllPositions')).lower()!='true'):return None
    try:
        quantity,closed,entry_qty=map(float,(r['positionAmt'],r['closePositionAmt'],entry['executedQty']))
        open_price,price,commission,funding=map(float,(r['avgPrice'],r['avgClosePrice'],r['positionCommission'],r.get('totalFunding')or 0))
        opened,ended=float(r['openTime'])/1000,float(r['updateTime'])/1000
        values=(quantity,closed,entry_qty,open_price,price,commission,funding,opened,ended)
        if not all(math.isfinite(v)for v in values):return None
        tolerance=max(1e-12,pos.qty*1e-10)
        if min(quantity,closed,entry_qty,price,open_price)<=0:return None
        if any(abs(v-pos.qty)>tolerance for v in(quantity,closed,entry_qty)):return None
        if abs(open_price-pos.entry)>max(1e-12,pos.entry*1e-10):return None
        if opened>ended or ended>now+1 or opened<pos.opened_at-60:return None
        gross=(price-open_price)*quantity*(1 if pos.side=='LONG'else -1)
        # History exposes realisedProfit rounded to four decimals. Check its
        # independently calculated gross without treating netProfit's rounded
        # display value as the source of precise trading cash.
        if abs(gross-float(r['realisedProfit']))>.0001000001:return None
        if commission>0:return None  # rebates need an explicit signed-fee path
        fees=-commission
        return dict(positionId=pid,qty=quantity,entry=open_price,exit=price,gross=gross,
                    fees=fees,net=gross-fees,funding=funding,closedAt=ended,
                    origin='external-position-close')
    except (ValueError,TypeError,KeyError,OverflowError):return None
