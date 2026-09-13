"""Shared venue protection with independent per-Set position accounting."""
from copy import copy
import math
import json
import os
import sys
import threading
import time

from position_cost import row_fee_usdt

FIELDS = ('sl_oid', 'tp_oid', 'sec_sl_oid', 'sec_tp_oid')


def cleanup_state(pulse):
    if not hasattr(pulse, '_overall_cleanup'):
        # The open-book path is connection-scoped, including in test fixtures.
        path = sys.modules[type(pulse).__module__].OPEN_PATH + '.controls'
        pulse._overall_cleanup_path = path
        try:
            with open(path) as f:
                data = json.load(f)
            pulse._overall_cleanup = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pulse._overall_cleanup = {}
    return pulse._overall_cleanup


def save_cleanup(pulse):
    path = pulse._overall_cleanup_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path+'.tmp','w') as f:
        json.dump(pulse._overall_cleanup,f)
    os.replace(path+'.tmp',path)


def drain_cleanup(pulse):
    pending = cleanup_state(pulse)
    if not pending or time.monotonic() < getattr(pulse,'_overall_final_cleanup_next',0):
        return
    pulse._overall_final_cleanup_next = time.monotonic()+1
    active = {getattr(p,f,'') for p in pulse.open.values() for f in FIELDS}-{''}
    for oid,symbol in list(pending.items())[:2]:
        if oid not in active and pulse.cancel_order(symbol,oid):
            pending.pop(oid,None)
            save_cleanup(pulse)


def closed_member(pulse,pos):
    """Retain cancellation intent after the last lot disappears from the book."""
    rows = [p for p in pulse.open.values() if p is not pos and p.symbol == pos.symbol
            and p.side == pos.side and pulse.position_is_ours(p) and p.qty > 0]
    ids = ({getattr(pos,f,'') for f in FIELDS} | set(pos.retired_control_ids))-{''}
    cache = getattr(pulse,'_overall_pairs',{})
    cache.pop((pos.symbol,pos.side),None)
    if rows:
        rows[0].retired_control_ids = sorted(set(rows[0].retired_control_ids) | ids)
        rows[0].overall_bindings = {**pos.overall_bindings, **rows[0].overall_bindings}
    else:
        pending = cleanup_state(pulse)
        pending.update({oid:pos.symbol for oid in ids})
        save_cleanup(pulse)


def enabled(pulse, pos=None):
    return bool(getattr(pulse, 'control_orders_overall', False)) and not bool(getattr(pos, '_overall_proxy', False))


def members(pulse, pos):
    rows = [p for p in list(pulse.open.values()) if p.symbol == pos.symbol and p.side == pos.side
            and pulse.position_is_ours(p) and p.qty > 0]
    if not any(p is pos for p in rows) and pos.qty > 0 and pulse.position_is_ours(pos):
        rows.append(pos)
    return rows


def drain_retired(pulse, rows):
    if time.monotonic() < getattr(pulse,'_overall_cleanup_next',0):
        return
    active = {getattr(p,f,'') for p in rows for f in FIELDS}-{''}
    pending = {oid for p in rows for oid in getattr(p,'retired_control_ids',[]) if oid and oid not in active}
    if not pending:
        return
    pulse._overall_cleanup_next = time.monotonic()+1
    changed = False
    for oid in sorted(pending)[:2]:
        if pulse.cancel_order(rows[0].symbol,oid):
            for p in rows:
                p.retired_control_ids = [v for v in getattr(p,'retired_control_ids',[]) if v != oid]
            changed = True
    if changed:
        pulse.save_open_book()


def replace_existing(pulse, proxy, rows, signature):
    """Use the venue cancel/replace operation without needing two spare slots.

    Each successful leg is saved immediately. A failed second leg never
    discards the first confirmed replacement or the other members' controls.
    """
    if time.monotonic() < getattr(pulse,'_overall_replace_next',{}).get((proxy.symbol,proxy.side),0):
        return False
    for field,kind,code,price in (('sl_oid','sl','u',proxy.sl),('tp_oid','tp','v',proxy.tp)):
        attr = 'overall_'+kind+'_signature'
        leg_sig = [signature[0],signature[1 if kind=='sl' else 2]]
        if all(getattr(p,attr,[]) == leg_sig and getattr(p,field,'') == getattr(rows[0],field,'') for p in rows) and getattr(rows[0],field,''):
            setattr(proxy,field,getattr(rows[0],field))
            continue
        old_oid = next((getattr(p,field,'') for p in rows if getattr(p,field,'')), '')
        intent = next((p.overall_replace_intents.get(kind) for p in rows if p.overall_replace_intents.get(kind)),None)
        response = None
        if intent:
            # Recover an ambiguous response before any resubmission. The
            # accepted order's client ID and original member sizes are fixed.
            found = pulse.api.get('/openApi/swap/v2/trade/order',{'symbol':proxy.symbol,'clientOrderId':intent['body']['clientOrderId']})
            data = found.get('data') or {}
            found_order = data.get('order',data) if isinstance(data,dict) else {}
            found_oid = str(found_order.get('orderId') or '')
            if pulse.ok(found) and found_oid and pulse.cid_ours(pulse.order_cid(found_order)) and pulse.order_cid(found_order).lower() == intent['body']['clientOrderId'].lower():
                response = {'code':0,'data':{'cancelResult':'SUCCESS','newOrderResult':'SUCCESS','newOrderId':found_oid}}
            else:
                absent = str(found.get('code')) == '109421' and 'not exist' in str(found.get('msg') or '').lower()
                prior = pulse.api.get('/openApi/swap/v2/trade/order',{'symbol':proxy.symbol,'orderId':intent['body']['cancelOrderId']}) if absent else {}
                prior_data = prior.get('data') or {}
                prior_order = prior_data.get('order',prior_data) if isinstance(prior_data,dict) else {}
                if not (pulse.ok(prior) and prior_order.get('status') == 'NEW' and str(prior_order.get('orderId')) == str(intent['body']['cancelOrderId'])):
                    return False  # Unresolved acknowledgement; keep polling.
                response = pulse.api.post('/openApi/swap/v1/trade/cancelReplace',intent['body'])
            body = intent['body'];old_oid = str(body['cancelOrderId'])
            leg_sig = intent['signature']
        else:
            body = pulse._ctrl_body(proxy,kind,price)
            body.pop('clientOrderID',None)
            body.update(cancelOrderId=old_oid, cancelReplaceMode='STOP_ON_FAILURE', clientOrderId=pulse.cid(code,pos=proxy))
            intent = {'body':dict(body),'signature':list(leg_sig),'binding':{p.client_id:p.qty for p in rows}}
            if old_oid:
                for p in rows:p.overall_replace_intents[kind]=dict(intent)
                pulse.save_open_book()
                response = pulse.api.post('/openApi/swap/v1/trade/cancelReplace',body)
            else:
                created = pulse.place_ctrl(proxy,'sec-'+kind,price)
                response = {'code':0,'data':{'cancelResult':'SUCCESS','newOrderResult':'SUCCESS','newOrderId':created}}
        data = response.get('data') or {}
        new_oid = str(data.get('newOrderId') or '') if isinstance(data,dict) else ''
        confirmed = pulse.ok(response) and data.get('cancelResult') == 'SUCCESS' and data.get('newOrderResult') == 'SUCCESS' and new_oid
        if not confirmed:
            # Explicit rejections can be rebuilt; transport uncertainty keeps
            # its exact pending intent for reconciliation.
            if response.get('code') not in (-1,'-1',None):
                for p in rows:p.overall_replace_intents.pop(kind,None)
                pulse.save_open_book()
            if isinstance(data,dict) and data.get('cancelResult') == 'SUCCESS':
                for p in rows:
                    for f in FIELDS:
                        if getattr(p,f,'') == old_oid:setattr(p,f,'')
                    p.controls_ok = False
                pulse.save_open_book()
            if not hasattr(pulse,'_overall_replace_next'):pulse._overall_replace_next={}
            pulse._overall_replace_next[(proxy.symbol,proxy.side)] = time.monotonic()+15
            pulse.last_error = 'overall replacement '+str(response.get('code'))+' '+str(response.get('msg') or '')[:120]
            return False
        binding = intent['binding']
        for p in rows:
            p.overall_replace_intents.pop(kind,None)
            old = getattr(p,field,'')
            if old and old != old_oid:
                p.retired_control_ids = sorted(set(p.retired_control_ids) | {old})
            if p is rows[0]:
                p.overall_bindings = {**p.overall_bindings,new_oid:dict(binding)}
            setattr(p,field,new_oid)
            setattr(p,'sec_'+field,new_oid)
            setattr(p,attr,list(leg_sig))
            p.overall_controls = True
        setattr(proxy,field,new_oid)
        pulse._oo_cache.pop('*',None)
        pulse.save_open_book()
        if leg_sig != [signature[0],signature[1 if kind=='sl' else 2]]:
            return False  # Recovered an older size; next pass resizes it.
    return True


def verify_pair(pulse,rows,current):
    if time.monotonic() < current.get('verify_after',0):
        return
    current['verify_after'] = time.monotonic()+15
    for oid in (current['sl'],current['tp']):
        result = pulse.api.get('/openApi/swap/v2/trade/order',{'symbol':rows[0].symbol,'orderId':oid})
        data = result.get('data') or {}
        order = data.get('order',data) if isinstance(data,dict) else {}
        if not pulse.ok(result) or str(order.get('orderId')) != oid:
            continue
        if order.get('status') in ('CANCELED','CANCELLED','EXPIRED','REJECTED') and float(order.get('executedQty') or 0) == 0:
            for p in rows:
                for field in FIELDS:
                    if getattr(p,field,'') == oid:setattr(p,field,'')
                p.controls_ok = False
            pulse.save_open_book()


def ensure(pulse, pos):
    if not getattr(pulse, 'control_orders', True) or getattr(pulse, '_overall_applying_fill', False):
        return
    lock = getattr(pulse, '_overall_control_lock', None)
    if lock is None:
        lock = pulse._overall_control_lock = threading.RLock()
    with lock:
        rows = members(pulse, pos)
        if not rows:
            return
        drain_retired(pulse,rows)
        key = (pos.symbol, pos.side)
        cache = getattr(pulse, '_overall_pairs', None)
        if cache is None:
            cache = pulse._overall_pairs = {}
        qty = sum(p.qty for p in rows)
        entry = sum(p.qty*p.entry for p in rows)/qty
        # The common exchange orders are an outer protection boundary. Each
        # member's exact SL/TP/trailing remains managed by the system.
        low = min(p.sl for p in rows if p.sl > 0)
        high = max(p.tp for p in rows if p.tp > 0)
        if pos.side == 'SHORT':
            low = max(p.sl for p in rows if p.sl > 0)
            high = min(p.tp for p in rows if p.tp > 0)
        signature = (round(qty,12), round(low,12), round(high,12))
        current = cache.get(key)
        if current:
            verify_pair(pulse,rows,current)
        if current and current['signature'] == signature and all(p.sl_oid == current['sl'] and p.tp_oid == current['tp'] for p in rows):
            return
        # On restart, persisted shared IDs are valid only when every member
        # agrees and the recorded total quantity still matches.
        if not current and all(getattr(p,'overall_controls',False) and p.sl_oid and p.tp_oid
                and p.sl_oid == rows[0].sl_oid and p.tp_oid == rows[0].tp_oid
                and tuple(getattr(p,'overall_signature',[])) == signature for p in rows):
            cache[key] = dict(signature=signature, sl=rows[0].sl_oid, tp=rows[0].tp_oid,verify_after=time.monotonic()+15)
            return
        proxy = copy(pos)
        proxy._overall_proxy = True
        proxy.qty, proxy.entry = qty, entry
        proxy.sl_pct = abs(entry-low)/entry
        proxy.tp_pct = abs(high-entry)/entry
        proxy.sl, proxy.tp = low, high
        proxy.sl_oid = proxy.tp_oid = proxy.sec_sl_oid = proxy.sec_tp_oid = ''
        proxy.ctrl_qty = 0
        proxy.ctrl_verified = proxy.controls_ok = False
        proxy.set_id = 'overall:'+pos.symbol+':'+pos.side
        proxy.set_idx = -1
        proxy.execution_lane = proxy.set_id
        proxy.legacy_aggregate = False
        proxy.control_group_key = ''
        proxy.control_range_key = ''
        proxy.control_sl_bp = proxy.control_tp_bp = 0
        pulse.prepare_position_group(proxy)
        existing_pair = any(p.sl_oid or p.tp_oid for p in rows)
        if existing_pair:
            if not replace_existing(pulse,proxy,rows,signature):
                return
        else:
            pulse.place_ctrl_pair(proxy)
        if not (proxy.sl_oid and proxy.tp_oid):
            # Keep previous confirmed protection when a replacement fails.
            for oid in {proxy.sl_oid,proxy.tp_oid}-{''}:
                if not pulse.cancel_order(pos.symbol,oid):
                    rows[0].retired_control_ids = sorted(set(rows[0].retired_control_ids) | {oid})
            pulse.save_open_book()
            return
        cache[key] = dict(signature=signature,sl=proxy.sl_oid,tp=proxy.tp_oid,verify_after=time.monotonic()+15)
        bindings = {}
        for p in rows:
            bindings.update(getattr(p,'overall_bindings',{}))
        binding = {p.client_id:p.qty for p in rows}
        bindings[proxy.sl_oid] = dict(binding)
        bindings[proxy.tp_oid] = dict(binding)
        for p in rows:
            p.overall_bindings = dict(bindings) if p is rows[0] else {}
            p.overall_signature = list(signature)
            p.overall_sl_signature = [signature[0],signature[1]]
            p.overall_tp_signature = [signature[0],signature[2]]
            retired = set(getattr(p,'retired_control_ids',[])) | {getattr(p,f,'') for f in FIELDS}
            p.retired_control_ids = sorted(retired - {'',proxy.sl_oid,proxy.tp_oid})
            p.sl_oid = p.sec_sl_oid = proxy.sl_oid
            p.tp_oid = p.sec_tp_oid = proxy.tp_oid
            p.overall_controls = True
            p.overall_qty = qty
            p.overall_sl, p.overall_tp = proxy.sl, proxy.tp
            p.controls_ok = p.ctrl_verified = True
            p.ctrl_qty = p.qty
        # Install first, retire only known old own IDs after confirmation.
        pulse.save_open_book()
        drain_retired(pulse,rows)


def sync_fill(pulse, order, cid, oid, executed, px, track):
    """Allocate a confirmed cumulative shared fill to its bound own lots."""
    old = pulse.pending_orders.get(cid) or {}
    meta = dict(old.get('metadata') or {})
    binding = meta.get('overall_members')
    if not binding:
        rows = [p for p in list(pulse.open.values()) if (getattr(p,'overall_controls',False) or oid in getattr(p,'overall_bindings',{}))
                and pulse.position_is_ours(p) and p.symbol == order.get('symbol')
                and p.side == order.get('positionSide') and oid in (set(getattr(p,'overall_bindings',{})) | {getattr(p,f,'') for f in FIELDS} | set(getattr(p,'retired_control_ids',[])))]
        if not rows:
            return None
        recorded = [p.overall_bindings[oid] for p in rows if oid in getattr(p,'overall_bindings',{})]
        if recorded and any(b != recorded[0] for b in recorded):
            return False
        binding = dict(recorded[0]) if recorded else {p.client_id:p.qty for p in rows}
        if '' in binding or not binding or any(not math.isfinite(q) or q <= 0 for q in binding.values()):
            return False
    if old.get('order_id') and str(old['order_id']) != oid:
        return False
    previous = float(old.get('filled_qty') or 0)
    total = sum(binding.values())
    if executed <= previous or executed > total+1e-9:
        return False
    delta = executed-previous
    fill_px = (executed*px-previous*float(old.get('avg_price') or 0))/delta
    if not math.isfinite(fill_px) or fill_px <= 0:
        return False
    applied = dict(meta.get('overall_applied') or {})
    applied_values = dict(meta.get('overall_applied_values') or {})
    applied_fees = dict(meta.get('overall_applied_fees') or {})
    previous_fee = float(old.get('fee_total') or 0)
    fee = max(previous_fee,row_fee_usdt(order))
    plan = []
    for parent, original in binding.items():
        target = executed*original/total
        done = max(previous*original/total,float(applied.get(parent,0)))
        quantity = target-done
        if quantity <= 1e-12:
            continue
        pos = pulse._position_for_client(parent)
        if pos is None or not pulse.position_is_ours(pos) or pos.symbol != order.get('symbol') or pos.side != order.get('positionSide') or quantity > pos.qty+1e-9:
            return False
        old_value = float(applied_values.get(parent,done*float(old.get('avg_price') or 0)))
        price = (target*px-old_value)/quantity
        if not math.isfinite(price) or price <= 0:
            return False
        exit_fee = max(0, fee*original/total-float(applied_fees.get(parent,previous_fee*original/total)))
        plan.append((pos,quantity,price,exit_fee,target))
    def checkpoint(complete=False):
        pulse._remember_pending(kind='close',cid=cid,symbol=order['symbol'],side=order['positionSide'],
            requested_qty=total,filled_qty=executed if complete else previous,order_id=oid,
            avg_price=px if complete else float(old.get('avg_price') or 0),fee_total=fee if complete else previous_fee,
            metadata={**meta,'overall_members':binding,'confirmed_overall_fill':True,
                'overall_applied':dict(applied),'overall_applied_values':dict(applied_values),'overall_applied_fees':dict(applied_fees)})
    checkpoint()
    pulse._overall_applying_fill = True
    try:
        for pos,quantity,price,exit_fee,target in plan:
            if not pulse._record_close_fill(pos,quantity,price,'exchange-overall-control',exchange=True,
                close_cid=cid+':'+pos.client_id,close_oid=oid,status='confirmed' if executed>=total else 'partial',
                cumulative_qty=target,exit_fee=exit_fee):
                return False
            applied[pos.client_id] = target
            applied_values[pos.client_id] = target*px
            applied_fees[pos.client_id] = fee*binding[pos.client_id]/total
            checkpoint()
        checkpoint(True)
        if executed >= total-1e-9:
            pulse._clear_pending(cid)
            pulse.seen_fill_cids.add(cid)
    finally:
        pulse._overall_applying_fill = False
    drain_cleanup(pulse)
    remaining = [p for p,*rest in plan if p.qty > 1e-9]
    cache = getattr(pulse,'_overall_pairs',{})
    cache.pop((order['symbol'],order['positionSide']),None)
    if remaining:
        ensure(pulse,remaining[0])
    return True
