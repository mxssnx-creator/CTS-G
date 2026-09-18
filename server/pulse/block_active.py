"""Adjusted-only Block entry planning. No exchange calls or synthetic fills.

The normal quantity is a reference, never an order. Shadow anchors are volatile:
after restart or a stale signal the continuation observation starts again.
"""
import math


class ContinuationBook(dict):
    """Expire the shared book once per second, not once per Set candidate."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sweep_at = float("-inf")


def executable_parent_qty(reference_qty, min_qty=0.0):
    """Venue-executable parent. Min-lot lifts intern crumbs; empty parents stay empty."""
    try:
        parent = float(reference_qty or 0.0)
    except (TypeError, ValueError):
        parent = 0.0
    try:
        floor = float(min_qty or 0.0)
    except (TypeError, ValueError):
        floor = 0.0
    if not math.isfinite(parent) or parent <= 0:
        return 0.0
    if not math.isfinite(floor) or floor < 0:
        floor = 0.0
    return max(parent, floor)


def adjusted_quantity(reference_qty, increment, owned_qty=0, pending_qty=0, min_qty=0, extra_cap=1.0):
    values = (reference_qty, increment, owned_qty, pending_qty, min_qty, extra_cap)
    try:
        parsed = [float(v) for v in values]
    except (TypeError, ValueError):
        return 0.0
    if any(not math.isfinite(v) or v < 0 for v in parsed):
        return 0.0
    parent = executable_parent_qty(reference_qty, min_qty)
    if parent <= 0:
        return 0.0
    inc = min(1.0, float(increment))
    cap = parent * max(0.0, float(extra_cap))
    target = min(parent * inc, cap)
    remaining = max(0.0, target - float(owned_qty) - float(pending_qty))
    room = max(0.0, cap - float(owned_qty) - float(pending_qty))
    if remaining <= 0 or room <= 0:
        return 0.0
    floor = float(min_qty or 0.0)
    if floor > 0 and remaining + 1e-12 < floor:
        if floor <= room + 1e-12:
            remaining = floor
        else:
            return 0.0
    return min(remaining, room)


def observe_continuation(anchors, key, price, direction, now):
    if not math.isfinite(price) or price <= 0 or direction not in (-1, 1):
        return False
    # Bound memory and discard abandoned signals. No state means no entry.
    if not isinstance(anchors, ContinuationBook) or now < anchors.sweep_at or now - anchors.sweep_at >= 1:
        for stale in [k for k, v in anchors.items() if now - v['seen'] > 180]:
            del anchors[stale]
        if isinstance(anchors, ContinuationBook):
            anchors.sweep_at = now
    prior = anchors.get(key)
    if prior is None or prior['direction'] != direction or now < prior['at'] or now - prior['seen'] > 180:
        anchors[key] = {'price': price, 'direction': direction, 'at': now, 'seen': now}
        return False
    prior['seen'] = now
    move = (price / prior['price'] - 1) * direction
    if move < 0:
        anchors.pop(key, None)
        return False
    return now - prior['at'] >= 45 and move >= .002
