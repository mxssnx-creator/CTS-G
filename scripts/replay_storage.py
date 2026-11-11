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
