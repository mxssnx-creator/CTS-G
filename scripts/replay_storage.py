"""Lossless packed signal columns for large, trusted replay snapshots."""
from array import array


def compact_shadow(shadow):
    """Release Python list/object overhead without changing one numeric value.

    Confidences remain IEEE binary64, exactly as Python floats. Directions use
    signed int32; trade and pause columns keep their original objects. Work one
    symbol at a time so the old universe is not retained during conversion.
    """
    for symbol in shadow:
        warm, directions, confidences, rows = shadow[symbol]
        shadow[symbol] = (warm,
            {p: v if isinstance(v,array) and v.typecode=='i' else array('i',v)
             for p,v in directions.items()},
            {p: v if isinstance(v,array) and v.typecode=='d' else array('d',v)
             for p,v in confidences.items()}, rows)
    return shadow


def release_shadow_trades(shadow):
    """Drop per-Set shadow trade columns after gate evaluation.

    The merged Tapes own the histories used by execution gates. The live
    execution walk only needs each symbol's pause plan, directions, and
    confidences, so retaining a second copy of every Set's trades wastes RAM.
    """
    for symbol, (warm, directions, confidences, rows) in shadow.items():
        pauses = rows.get(-1, {})
        shadow[symbol] = (warm, directions, confidences, {-1: pauses})
    return shadow
