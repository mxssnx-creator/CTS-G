"""Contract suite: the sidecar payloads and the desk types describe the same fields.

The desk reads the overall snapshot and each lane summary through the types in src/lib/live-stats.ts. A key the
sidecar sends that the type does not declare is invisible to the type checker, so the drift is checked here. The
nullable overall fields must be declared nullable and must be null or a number at runtime.
"""
from __future__ import annotations

import os
import re
import tempfile

from common import ROOT, Skip  # noqa: F401
import pulse_http as h

TS_PATH = os.path.join(ROOT, "src", "lib", "live-stats.ts")
NULLABLE_OVERALL = ("usedMargin", "pnlPct", "drawdownPct", "maxOpen")


def _ts() -> str:
    if not os.path.exists(TS_PATH):
        raise Skip("src/lib/live-stats.ts is not in this checkout")
    with open(TS_PATH, encoding="utf-8") as f:
        return f.read()


def _top_declarations(ts: str) -> dict:
    """LiveStats top-level field name -> its declaration line (two-space indent inside the type)."""
    i = ts.index("export type LiveStats = {")
    j = ts.index("\n};", i)
    return {m.group(1): m.group(0) for m in re.finditer(r"^  ([A-Za-z_]\w*)\??:[^\n]*", ts[i:j], re.M)}


def _lane_fields(ts: str) -> set:
    """Field names of the overall snapshot's lane entries (the lanes array at two-space indent)."""
    start = re.search(r"^  lanes\?: Array<\{", ts, re.M).start()
    end = ts.index("\n  }>;", start)
    return set(re.findall(r"^    ([A-Za-z_]\w*)\??:", ts[start:end], re.M))


def _payloads():
    """The overall snapshot and one lane summary, built from two lanes with one open position and one close each."""
    saved = (h.stats_age, h.unit_state, h.load_stats, h.DIR)
    stats = {
        "bingx-x01": {"running": True, "equity": 100.0, "sessionPnl": 1.0, "usedMargin": 5.0,
                      "open": [{"symbol": "BTC-USDT", "clientId": "Gx01a", "unit": "USDT"}],
                      "closed": [{"symbol": "BTC-USDT", "pnl": 1.0, "t": 1}]},
        "bingx-x02": {"running": True, "equity": 300.0, "sessionPnl": -1.0,
                      "open": [], "closed": [{"symbol": "ETH-USDT", "pnl": -1.0, "t": 2}]},
    }
    try:
        with tempfile.TemporaryDirectory() as td:
            h.DIR = td
            h.unit_state = lambda conn, fresh=False: "active"
            h.stats_age = lambda conn: 1.0
            h.load_stats = lambda conn: dict(stats.get(conn) or {})
            return h.merge_overall(), h.lane_summary(h.LANES[0])
    finally:
        h.stats_age, h.unit_state, h.load_stats, h.DIR = saved


def overall_keys_are_declared():
    overall, _ = _payloads()
    missing = sorted(set(overall) - set(_top_declarations(_ts())))
    return not missing, f"overall keys the desk type does not declare: {missing or 'none'}"


def lane_keys_are_declared():
    _, lane = _payloads()
    missing = sorted(set(lane) - _lane_fields(_ts()))
    return not missing, f"lane keys the desk type does not declare: {missing or 'none'}"


def nullable_overall_fields_are_declared_and_typed():
    """Declared as number | null, and at runtime null or a number. A silent lane gives null, never 0 or a string."""
    overall, _ = _payloads()
    decl = _top_declarations(_ts())
    not_nullable = [k for k in NULLABLE_OVERALL if k not in decl or "| null" not in decl[k]]
    bad_values = {k: overall.get(k) for k in NULLABLE_OVERALL
                  if overall.get(k) is not None and not isinstance(overall.get(k), (int, float))}
    ok = not not_nullable and not bad_values
    return ok, f"not declared nullable: {not_nullable or 'none'}; non-number values: {bad_values or 'none'}"


def overall_closed_rows_name_their_lane():
    """Each closed row says which lane closed it, so a lane view can filter on it."""
    overall, _ = _payloads()
    rows = overall.get("closed") or []
    conns = sorted({str(r.get("connection")) for r in rows})
    ok = bool(rows) and all(r.get("connection") in ("bingx-x01", "bingx-x02") for r in rows) \
        and conns == ["bingx-x01", "bingx-x02"]
    return ok, f"closed rows={len(rows)} connections={conns}"


CHECKS = [
    ("contract.overall-keys-declared", overall_keys_are_declared),
    ("contract.lane-keys-declared", lane_keys_are_declared),
    ("contract.nullable-overall-fields", nullable_overall_fields_are_declared_and_typed),
    ("contract.closed-rows-name-their-lane", overall_closed_rows_name_their_lane),
]
