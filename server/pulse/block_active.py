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


def adjusted_quantity(reference_qty, increment, owned_qty=0, pending_qty=0):
    values = (reference_qty, increment, owned_qty, pending_qty)
    if any(not math.isfinite(float(v)) or float(v) < 0 for v in values):
        return 0.0
    return max(0.0, float(reference_qty) * min(1.0, float(increment))
               - float(owned_qty) - float(pending_qty))


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
