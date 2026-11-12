"""Exact commission reconciliation for a fully settled owned position.

Entry/control paths retain their available fee evidence without another HTTP
round trip. At final settlement, complete identity-bound fills can replace the
rate estimates. A missing or partial ledger cannot claim reported fees.
"""
from decimal import Decimal, InvalidOperation
import math


def verified_bingx_fee(reply, leg):
    """A signed commission is a credit; negative commission is a fee debit."""
    try:
        if str(reply.get('code')) != '0' or reply.get('error'):
            return None
        rows = reply['data']['fill_orders']
        if not isinstance(rows,list) or not rows:
            return None
        qty=value=commission=Decimal(0)
        oid=str(leg['orderId'])
        for row in rows:
            if (str(row.get('orderId')) != oid and str(row.get('triggerOrderId')) != oid
                    or row.get('symbol') != leg['symbol']
                    or row.get('positionSide') != leg['positionSide']
                    or row.get('side') != leg['side']
                    or row.get('currency') not in ('USDT','VST')):
                return None
            q,p,c=(Decimal(str(row[k])) for k in ('volume','price','commission'))
            if not all(x.is_finite() for x in (q,p,c)) or q<=0 or p<=0:
                return None
            qty+=q;value+=q*p;commission+=c
        requested=Decimal(str(leg['qty']))
        expected=Decimal(str(leg['price']))*requested
        if abs(qty-requested)>max(Decimal('1e-12'),requested*Decimal('1e-10')):
            return None
        if abs(value-expected)>max(Decimal('1e-10'),abs(expected)*Decimal('1e-9')):
            return None
        return float(-commission)
    except (KeyError,TypeError,ValueError,InvalidOperation,AttributeError):
        return None


def remember_fee_leg(pos, data, qty, price, fee, source):
    """Persist cumulative order scope; repeated control polls replace one leg."""
    if pos is None or not isinstance(data,dict):return
    oid=str(data.get('orderId') or data.get('orderID') or '')
    side=str(data.get('side') or '').upper()
    if (not oid.isdigit() or int(oid)<=0 or side not in ('BUY','SELL')
            or str(data.get('symbol') or '')!=pos.symbol
            or str(data.get('positionSide') or '').upper()!=pos.side
            or not all(math.isfinite(float(x)) for x in (qty,price,fee)) or qty<=0 or price<=0):
        return
    if not hasattr(pos,'fee_legs'):pos.fee_legs={}
    previous=pos.fee_legs.get(oid)
    if previous and float(previous['qty'])>qty+max(1e-12,qty*1e-10):return
    pos.fee_legs[oid]=dict(orderId=oid,symbol=pos.symbol,positionSide=pos.side,side=side,
        qty=qty,price=price,fee=fee,source=source,
        role='entry' if side==('BUY' if pos.side=='LONG' else 'SELL') else 'exit')


def reconcile_position_fees(pos, reader):
    """Replace complete fee totals atomically, never add another deduction."""
    legs=getattr(pos,'fee_legs',{})
    if not callable(reader) or not legs or pos.entry_fee_usdt is None:return False
    tolerance=max(1e-12,pos.qty*1e-10)
    if any(abs(sum(float(r['qty']) for r in legs.values() if r['role']==role)-pos.qty)>tolerance
           for role in ('entry','exit')):
        return False
    reported={}
    for oid,leg in legs.items():
        try:
            fee=leg['fee'] if leg['source']=='reported-fee' else reader(leg,pos.opened_at)
            if fee is None or not math.isfinite(float(fee)):return False
            reported[oid]=float(fee)
        except Exception:
            return False
    pos.entry_fee_usdt=sum(reported[k] for k,r in legs.items() if r['role']=='entry')
    pos.exit_fee_usdt=sum(reported[k] for k,r in legs.items() if r['role']=='exit')
    if str(pos.order_id) in reported:
        pos.base_entry_fee_usdt=reported[str(pos.order_id)]
    for oid,fee in reported.items():
        legs[oid].update(fee=fee,source='reported-fee')
        if oid in pos.control_fill_state:pos.control_fill_state[oid]['fee']=fee
    pos.fee_source='reported-fee'
    return True
