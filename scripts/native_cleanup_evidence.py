"""Bounded, read-only terminal evidence for exact journaled VST orders."""
from concurrent.futures import ThreadPoolExecutor, wait
import time

TERMINAL = {'FILLED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}


def definite_refusal(reply):
    code = str(reply.get('code'))
    no_position = code == '101205' and str(reply.get('msg') or '').strip().lower() == 'no position to close'
    return reply.get('cooled') is True or ((code in ('101400','109420') or no_position)
        and not reply.get('error') and not reply.get('data'))


def resolve_bound_intents(api, intents, unresolved, deadline, order_bodies, bindings, remember=None):
    """Read exact previously bound order IDs when the archive omits controls.

    A missing client-ID query cannot prove a write was rejected. Only a
    receipt bound to this submission supplies an alternative lookup identity.
    """
    remaining = set(unresolved)
    for intent in intents:
        if intent['method'] != 'POST' or definite_refusal(intent.get('response') or {}):
            continue
        data = (intent.get('response') or {}).get('data') or {}
        candidates = data.get('orders', [data.get('order', data)]) if isinstance(data, dict) else []
        for body in order_bodies(intent['path'], intent['body']):
            cid = body['clientOrderID']
            if cid not in remaining or time.monotonic() >= deadline:
                continue
            ids = {str(oid) for (symbol, oid), submitted in bindings.items()
                   if symbol == body['symbol'] and
                   str(submitted.get('clientOrderID') or submitted.get('clientOrderId') or '').lower() == cid.lower()}
            ids.update(str(row['orderId']) for row in candidates if isinstance(row, dict) and row.get('orderId') and
                       row.get('symbol') == body['symbol'] and
                       str(row.get('clientOrderID') or row.get('clientOrderId') or '').lower() == cid.lower())
            if len(ids) != 1:
                continue
            oid = next(iter(ids))
            try:
                reply = api.get('/openApi/swap/v2/trade/order', {'symbol': body['symbol'], 'orderId': oid})
                data = reply.get('data') or {}
                row = data.get('order', data) if isinstance(data, dict) else {}
                found = str(row.get('clientOrderID') or row.get('clientOrderId') or '')
                exact = (str(reply.get('code')) == '0' and not reply.get('error') and
                         str(row.get('orderId')) == oid and (not found or found.lower() == cid.lower()) and
                         all(row.get(key) == body.get(key) for key in ('symbol', 'side', 'positionSide', 'type')))
                final = exact and (str(row.get('status')).upper() in TERMINAL or
                    (body.get('type') != 'MARKET' and str(row.get('status')).upper() in ('NEW', 'PENDING', 'TRIGGERED')))
                if final:
                    if remember is not None:
                        remember(row, body)
                    remaining.remove(cid)
            except Exception:
                pass  # A failed read supplies no terminal evidence.
    return [cid for cid in unresolved if cid in remaining]


def terminal_intents(api, intents, deadline, order_bodies, remember=None):
    required = []
    starts = {}
    for intent in intents:
        if intent['method'] != 'POST':
            continue
        ack = intent.get('response') or {}
        if definite_refusal(ack):
            continue
        data = ack.get('data') or {}
        candidates = data.get('orders', [data.get('order', data)]) if isinstance(data, dict) else []
        for body in order_bodies(intent['path'], intent['body']):
            cid = body['clientOrderID']
            matches = [r for r in candidates if isinstance(r, dict) and
                       str(r.get('clientOrderID') or r.get('clientOrderId') or '').lower() == cid.lower()]
            oid = str(matches[0].get('orderId') or matches[0].get('orderID') or '') if len(matches) == 1 else ''
            required.append((body, oid))
            starts[body['symbol']] = min(starts.get(body['symbol'], float('inf')), intent['at'])
    if not required:
        return []
    histories = {}
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='cleanup-evidence')
    futures = {}
    try:
        for symbol, start in starts.items():
            if time.monotonic() >= deadline:
                break
            futures[pool.submit(api.get, '/openApi/swap/v2/trade/allOrders',
                {'symbol': symbol, 'startTime': int(max(0, start - 60)*1000),
                 'endTime': int(time.time()*1000), 'limit': 1000})] = symbol
        done, _ = wait(futures, timeout=max(0, deadline - time.monotonic()))
        for future in done:
            try:
                reply = future.result()
                data = reply.get('data') or {}
                rows = data.get('orders') if isinstance(data, dict) else None
                if str(reply.get('code')) == '0' and not reply.get('error') and isinstance(rows, list):
                    histories[futures[future]] = rows
            except Exception:
                pass  # Missing terminal evidence remains unresolved, never an empty success.
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    unresolved = []
    for body, oid in required:
        cid = body['clientOrderID']
        rows = [row for row in histories.get(body['symbol'], []) if isinstance(row, dict) and
                str(row.get('clientOrderID') or row.get('clientOrderId') or '').lower() == cid.lower() and
                (not oid or str(row.get('orderId') or row.get('orderID') or '') == oid)]
        row = rows[0] if len(rows) == 1 else None
        matched = row is not None and all(row.get(k) == body.get(k) for k in ('symbol', 'side', 'positionSide', 'type'))
        final = matched and (str(row.get('status')).upper() in TERMINAL or
            (body.get('type') != 'MARKET' and str(row.get('status')).upper() in ('NEW', 'PENDING', 'TRIGGERED')))
        if not final:
            unresolved.append(cid)
        elif remember is not None:
            remember(row, body)
    return unresolved
