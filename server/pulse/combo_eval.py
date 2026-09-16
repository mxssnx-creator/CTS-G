#!/usr/bin/env python3
"""Independent indication × type × config × strategy evaluation.

Calc-once: stream existing SetBook tapes (core Sets, indication kinds,
Block/DCA overlays) into an in-memory SQLite catalog. Each combination keeps
its own last-N cost PF. PF families and with/without Block or DCA are scored
from the same fills — never averaged across Sets.

Hot path never copies CompactHistRow into a dict. Set metadata is overlaid
on read so millions of fills stay in the compact representation.
"""
from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Mapping
from typing import Any, Dict, Iterable, List, Optional, Tuple

from position_cost import POSITIVE_PF, is_positive_pf, last_n_cost_pf
from set_engine import IND_KINDS, drawdown_time

STRATEGIES = ("normal", "trailing", "axis", "block", "dca")
PF_FAMILIES = ("overall", "normal", "trailing", "axis", "block", "dca")
INDICATIONS = ("general", "combined") + tuple(IND_KINDS)
TAIL_CAP = 80
SUCCESSFUL_CAP = 80
MATRIX_EMPTY = {"n": 0, "evalN": 0, "pf": 1.0, "wr": 0.0, "netAvg": 0.0, "validated": False, "maxDdS": 0.0, "avgDdS": 0.0, "pfDdRatio": 0.0}


def open_combo_db() -> sqlite3.Connection:
    """Ephemeral scoring DB: RAM only, no journal, no fsync, 16 MB page cache."""
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA page_size=4096")
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA cache_size=-16384")
    try:
        db.execute("PRAGMA cache_spill=OFF")
    except sqlite3.Error:
        pass
    db.execute("PRAGMA locking_mode=EXCLUSIVE")
    db.executescript(
        """
        CREATE TABLE combos (
            indication TEXT NOT NULL,
            config TEXT NOT NULL,
            strategy TEXT NOT NULL,
            set_id TEXT NOT NULL,
            sl REAL,
            step INTEGER,
            trail TEXT,
            n INTEGER NOT NULL,
            eval_n INTEGER NOT NULL,
            pf REAL NOT NULL,
            wr REAL NOT NULL,
            net_avg REAL NOT NULL,
            validated INTEGER NOT NULL
        );
        CREATE INDEX combos_lookup ON combos(indication, strategy, validated, pf DESC);
        """
    )
    return db


def db_pragmas(db: sqlite3.Connection) -> Dict[str, Any]:
    journal = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    temp = db.execute("PRAGMA temp_store").fetchone()[0]
    cache = db.execute("PRAGMA cache_size").fetchone()[0]
    return {
        "engine": "sqlite-memory",
        "journal": journal,
        "tempStore": "memory" if int(temp) == 2 else temp,
        "cachePages": int(cache),
        "path": str(db.execute("PRAGMA database_list").fetchone()[2] or ""),
    }


class _Acc:
    __slots__ = ("n", "wins", "decided", "hold", "tail")

    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.decided = 0
        self.hold = 0.0
        self.tail: deque = deque(maxlen=TAIL_CAP)

    def add(self, t: float, pnl_pct: float, hold: float) -> None:
        self.n += 1
        if pnl_pct > 0:
            self.wins += 1
        if pnl_pct != 0:
            self.decided += 1
        self.hold += hold
        self.tail.append({"t": t, "pnl_pct": pnl_pct})


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(key, default)
    if isinstance(row, Mapping):
        try:
            return row[key]
        except Exception:
            return default
    return default


def _pick(row: Any, meta: Optional[Dict[str, Any]], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = _row_get(row, key)
        if value not in (None, ""):
            return value
    if meta:
        for key in keys:
            value = meta.get(key)
            if value not in (None, ""):
                return value
    return default


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _indication_of(row: Any, meta: Optional[Dict[str, Any]] = None, pack: str = "") -> str:
    kind = str(_pick(row, meta, "ind_kind", "indKind", default="")).strip().lower()
    if kind in IND_KINDS:
        return kind
    reason = str(_pick(row, meta, "reason", default=""))
    if reason.startswith("ind:") or reason.startswith("block:"):
        bits = reason.split(":")
        cand = (bits[1] if len(bits) > 1 else "").strip().lower()
        if cand in IND_KINDS:
            return cand
    pack = str(pack or _pick(row, meta, "pack", default="") or "").lower()
    if pack == "general":
        return "general"
    if pack == "indications":
        return "combined"
    return pack or "combined"


def _strategy_of(row: Any, meta: Optional[Dict[str, Any]] = None) -> str:
    tagged = str(_pick(row, meta, "strategy", default="")).strip().lower()
    if tagged in STRATEGIES:
        return tagged
    pack = str(_pick(row, meta, "pack", default="") or "").lower()
    reason = str(_pick(row, meta, "reason", default="") or "").lower()
    if pack == "dca" or reason.startswith("dca") or tagged == "dca":
        return "dca"
    if pack == "block" or reason.startswith("block") or tagged == "block":
        return "block"
    if _pick(row, meta, "axis_key", "axisKey") or pack == "axis" or tagged == "axis":
        return "axis"
    trail = str(_pick(row, meta, "trail_key", "trailKey", default="") or "")
    kind = str(_pick(row, meta, "kind", default="") or "")
    if kind == "trail" or (trail and trail not in ("", "0", "off", "none")):
        return "trailing"
    return "normal"


def _config_of(row: Any, meta: Optional[Dict[str, Any]] = None) -> str:
    explicit = str(_pick(row, meta, "ind_config", "indConfig", default="")).strip()
    if explicit:
        return explicit
    sl = _f(_pick(row, meta, "sl_ratio", "slRatio", default=0))
    step = int(_f(_pick(row, meta, "step", default=0)))
    trail = str(_pick(row, meta, "trail_key", "trailKey", default="base") or "base")
    return f"sl{sl:.1f}:st{step}:tr{trail or 'base'}"


def _score(acc: _Acc, cost_pct: float, pf_n: int) -> Dict[str, Any]:
    tail = list(acc.tail)
    window = last_n_cost_pf(tail, max(1, pf_n), cost_pct, ordered=True, simple=True) if tail else last_n_cost_pf([], 1, cost_pct)
    pf = float(window.get("ratio") or 1.0)
    eval_n = int(window.get("count") or 0)
    wr = round(100.0 * acc.wins / acc.decided, 1) if acc.decided else 0.0
    dd = drawdown_time(tail, ordered=True) if tail else {"maxS": 0.0, "avgS": 0.0}
    max_dd = float(dd.get("maxS") or 0)
    return {
        "n": acc.n,
        "evalN": eval_n,
        "pf": round(pf, 4),
        "wr": wr,
        "netAvg": round(float(window.get("netAvg") or 0), 6),
        "validated": eval_n > 0 and is_positive_pf(pf),
        "costSubtracted": True,
        "maxDdS": round(max_dd, 1),
        "avgDdS": round(float(dd.get("avgS") or 0), 1),
        "pfDdRatio": round(pf / max(0.05, (max_dd / 3600.0) + 0.05), 4),
    }


def _empty_family() -> Dict[str, Any]:
    return dict(MATRIX_EMPTY, costSubtracted=True)


def _unwrap(item: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    if isinstance(item, tuple) and len(item) == 2:
        return item[0], item[1] if isinstance(item[1], dict) else None
    return item, None


def evaluate_fills(
    fills: Iterable[Any],
    *,
    min_pf: float = POSITIVE_PF,
    cost_pct: float = 0.1,
    pf_n: int = 30,
) -> Dict[str, Any]:
    """Score an already-materialised fill stream. Used by tests and evaluate_book."""
    combo_acc: Dict[Tuple[str, str, str, str], _Acc] = {}
    matrix_acc: Dict[Tuple[str, str], _Acc] = {}
    family_acc: Dict[str, _Acc] = {key: _Acc() for key in PF_FAMILIES}
    with_acc: Dict[str, Dict[str, _Acc]] = {
        "block": {"with": _Acc(), "without": _Acc()},
        "dca": {"with": _Acc(), "without": _Acc()},
    }
    combo_meta: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

    for item in fills:
        row, meta = _unwrap(item)
        if row is None:
            continue
        if not hasattr(row, "get") and not isinstance(row, Mapping):
            continue
        indication = _indication_of(row, meta)
        strategy = _strategy_of(row, meta)
        config = _config_of(row, meta)
        set_id = str(_pick(row, meta, "set_id", "setId", "id", default=f"{indication}:{config}:{strategy}"))
        t = _f(_pick(row, meta, "t", default=0))
        pnl_pct = _f(_pick(row, meta, "pnl_pct", "pnlPct", default=0))
        hold = _f(_pick(row, meta, "hold_s", "holdS", default=0))
        key = (indication, config, strategy, set_id)
        acc = combo_acc.get(key)
        if acc is None:
            acc = _Acc()
            combo_acc[key] = acc
            combo_meta[key] = {
                "sl": _f(_pick(row, meta, "sl_ratio", "slRatio", default=0)),
                "step": int(_f(_pick(row, meta, "step", default=0))),
                "trail": str(_pick(row, meta, "trail_key", "trailKey", default="")),
            }
        acc.add(t, pnl_pct, hold)
        mkey = (indication, strategy)
        matt = matrix_acc.get(mkey)
        if matt is None:
            matt = _Acc()
            matrix_acc[mkey] = matt
        matt.add(t, pnl_pct, hold)
        family_acc["overall"].add(t, pnl_pct, hold)
        if strategy in family_acc:
            family_acc[strategy].add(t, pnl_pct, hold)
        with_acc["block"]["with"].add(t, pnl_pct, hold)
        with_acc["dca"]["with"].add(t, pnl_pct, hold)
        if strategy != "block":
            with_acc["block"]["without"].add(t, pnl_pct, hold)
        if strategy != "dca":
            with_acc["dca"]["without"].add(t, pnl_pct, hold)

    db = open_combo_db()
    meta = db_pragmas(db)
    rows: List[Tuple] = []
    public_combos: List[Dict[str, Any]] = []
    for key, acc in combo_acc.items():
        indication, config, strategy, set_id = key
        scored = _score(acc, cost_pct, pf_n)
        info = combo_meta[key]
        rows.append(
            (
                indication,
                config,
                strategy,
                set_id,
                info["sl"],
                info["step"],
                info["trail"],
                scored["n"],
                scored["evalN"],
                scored["pf"],
                scored["wr"],
                scored["netAvg"],
                1 if scored["validated"] else 0,
            )
        )
        if scored["validated"]:
            public_combos.append(
                {
                    "indication": indication,
                    "config": config,
                    "strategy": strategy,
                    "setId": set_id,
                    "slRatio": info["sl"],
                    "step": info["step"],
                    "trailKey": info["trail"],
                    **scored,
                }
            )
    if rows:
        db.executemany("INSERT INTO combos VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()

    successful_sql = db.execute(
        "SELECT indication, config, strategy, set_id, sl, step, trail, n, eval_n, pf, wr, net_avg "
        "FROM combos WHERE validated=1 AND pf>=? AND n>0 ORDER BY pf DESC, n DESC LIMIT ?",
        (float(min_pf), SUCCESSFUL_CAP),
    ).fetchall()
    successful = [
        {
            "indication": r[0],
            "config": r[1],
            "strategy": r[2],
            "setId": r[3],
            "slRatio": r[4],
            "step": r[5],
            "trailKey": r[6],
            "n": r[7],
            "evalN": r[8],
            "pf": r[9],
            "wr": r[10],
            "netAvg": r[11],
            "validated": True,
        }
        for r in successful_sql
    ]
    by_combo = {c.get("setId"): c for c in public_combos}
    for row in successful:
        extra = by_combo.get(row["setId"]) or {}
        row["maxDdS"] = extra.get("maxDdS") or 0.0
        row["avgDdS"] = extra.get("avgDdS") or 0.0
        row["pfDdRatio"] = extra.get("pfDdRatio") or 0.0
    cell_count = int(db.execute("SELECT COUNT(*) FROM combos").fetchone()[0])
    validated_count = int(db.execute("SELECT COUNT(*) FROM combos WHERE validated=1").fetchone()[0])
    db.close()

    matrix: List[Dict[str, Any]] = []
    for indication in INDICATIONS:
        for strategy in STRATEGIES:
            acc = matrix_acc.get((indication, strategy))
            scored = _score(acc, cost_pct, pf_n) if acc is not None else dict(MATRIX_EMPTY)
            matrix.append({"indication": indication, "strategy": strategy, **scored})

    pf_stats = {name: _score(family_acc[name], cost_pct, pf_n) if family_acc[name].n else _empty_family() for name in PF_FAMILIES}
    with_without = {
        name: {
            "with": _score(pair["with"], cost_pct, pf_n) if pair["with"].n else _empty_family(),
            "without": _score(pair["without"], cost_pct, pf_n) if pair["without"].n else _empty_family(),
        }
        for name, pair in with_acc.items()
    }

    kinds_seen = {key[0] for key in combo_acc}
    strategies_seen = {key[2] for key in combo_acc}
    return {
        "ok": True,
        "minPf": float(min_pf),
        "costPct": float(cost_pct),
        "pfN": int(pf_n),
        "pfStats": pf_stats,
        "withWithout": with_without,
        "matrix": matrix,
        "successful": successful,
        "combos": public_combos[:SUCCESSFUL_CAP],
        "meta": {
            **meta,
            "cells": cell_count,
            "successfulCount": len(successful),
            "validatedCount": validated_count,
            "indications": sorted(kinds_seen),
            "strategies": sorted(strategies_seen),
            "coverage": {
                "indications": {k: k in kinds_seen for k in INDICATIONS},
                "strategies": {k: k in strategies_seen or family_acc[k].n > 0 for k in STRATEGIES},
                "families": {k: pf_stats[k]["n"] > 0 for k in PF_FAMILIES},
            },
        },
    }


def _set_meta(st: Any) -> Dict[str, Any]:
    pack = str(getattr(st, "pack", "") or "")
    trail = str(getattr(st, "trail_key", "") or "")
    kind = str(getattr(st, "kind", "") or "")
    return {
        "set_id": str(getattr(st, "id", "") or ""),
        "pack": pack,
        "trail_key": trail,
        "kind": kind,
        "sl_ratio": _f(getattr(st, "sl_ratio", 0)),
        "step": int(_f(getattr(st, "step", 0))),
        "strategy": "trailing" if kind == "trail" else "",
    }


def _set_fills(st: Any) -> Iterable[Tuple[Any, Dict[str, Any]]]:
    meta = _set_meta(st)
    for row in getattr(st, "hist", None) or []:
        if row is not None:
            yield row, meta


def iter_book_fills(book: Any) -> Iterable[Tuple[Any, Dict[str, Any]]]:
    """Stream every independent tape without concatenating into one giant list."""
    for st in getattr(book, "by_idx", None) or []:
        yield from _set_fills(st)
    for kind, tape in (getattr(book, "ind_hist", None) or {}).items():
        indication = str(kind).partition("|")[0]
        meta = {
            "ind_kind": indication,
            "pack": "indications",
            "strategy": "normal",
            "set_id": f"indications:{indication}",
        }
        for row in tape or []:
            if row is not None:
                yield row, meta
    for name, tape in (getattr(book, "strategy_hist", None) or {}).items():
        label = str(name or "")
        strategy = "dca" if label.startswith("dca") else "block" if "block" in label else _strategy_of({"strategy": label, "pack": label})
        indication = ""
        if ":" in label:
            tail = label.split(":", 1)[1]
            if tail in IND_KINDS:
                indication = tail
        meta = {
            "strategy": strategy,
            "ind_kind": indication,
            "pack": strategy,
            "set_id": label,
        }
        for row in tape or []:
            if row is not None:
                yield row, meta


def evaluate_book(
    book: Any,
    *,
    min_pf: float = POSITIVE_PF,
    cost_pct: Optional[float] = None,
    pf_n: Optional[int] = None,
) -> Dict[str, Any]:
    cost = float(cost_pct if cost_pct is not None else getattr(book, "cost_pct", 0.1) or 0.1)
    window = int(pf_n if pf_n is not None else getattr(book, "pf_n", 30) or 30)
    floor = float(min_pf if min_pf is not None else getattr(book, "min_pf", POSITIVE_PF) or POSITIVE_PF)
    return evaluate_fills(iter_book_fills(book), min_pf=floor, cost_pct=cost, pf_n=window)


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []

    def rec(name: str, ok: bool, detail: Any = "") -> None:
        out.append((name, bool(ok), str(detail)[:220]))

    db = open_combo_db()
    info = db_pragmas(db)
    rec("combo-db-memory-path", info["path"] == "", info["path"])
    rec("combo-db-journal", info["journal"] in ("memory", "off"), info["journal"])
    rec("combo-db-temp", info["tempStore"] == "memory", info["tempStore"])
    rec("combo-db-cache", int(info["cachePages"]) <= -8192, info["cachePages"])
    db.close()

    wins = [{"t": 1000 + i, "pnl_pct": 0.012, "ind_kind": "signals", "strategy": "normal", "set_id": "a", "ind_config": "c1", "sl_ratio": 0.6, "step": 8, "trail_key": ""} for i in range(20)]
    losses = [{"t": 2000 + i, "pnl_pct": -0.01, "ind_kind": "state", "strategy": "normal", "set_id": "b", "ind_config": "c1", "sl_ratio": 0.6, "step": 8} for i in range(20)]
    block = [{"t": 3000 + i, "pnl_pct": 0.02, "ind_kind": "signals", "strategy": "block", "set_id": "a", "reason": "block:signals:tp", "sl_ratio": 0.6, "step": 8} for i in range(12)]
    dca = [{"t": 4000 + i, "pnl_pct": -0.004, "ind_kind": "signals", "strategy": "dca", "set_id": "a", "reason": "dca:add", "sl_ratio": 0.6, "step": 8} for i in range(8)]
    trail = [{"t": 5000 + i, "pnl_pct": 0.008, "ind_kind": "general", "strategy": "trailing", "set_id": "c", "kind": "trail", "trail_key": "0.3/0.1", "sl_ratio": 0.6, "step": 8} for i in range(10)]
    blob = evaluate_fills(wins + losses + block + dca + trail, min_pf=1.1, cost_pct=0.1, pf_n=15)
    rec("combo-ok", bool(blob.get("ok")))
    rec("combo-families", set((blob.get("pfStats") or {})) == set(PF_FAMILIES), sorted((blob.get("pfStats") or {})))
    rec("combo-independent-kinds", float(((blob.get("pfStats") or {}).get("overall") or {}).get("n") or 0) == 70, (blob.get("pfStats") or {}).get("overall"))
    sig = next((c for c in blob.get("matrix") or [] if c["indication"] == "signals" and c["strategy"] == "normal"), None)
    stt = next((c for c in blob.get("matrix") or [] if c["indication"] == "state" and c["strategy"] == "normal"), None)
    rec("combo-signals-vs-state", bool(sig and stt and sig["pf"] != stt["pf"]), {"signals": sig, "state": stt})
    ww = blob.get("withWithout") or {}
    rec("combo-with-block-more-fills", int(((ww.get("block") or {}).get("with") or {}).get("n") or 0) > int(((ww.get("block") or {}).get("without") or {}).get("n") or 0), ww.get("block"))
    rec("combo-without-dca-excludes", int(((ww.get("dca") or {}).get("without") or {}).get("n") or 0) == 62, ww.get("dca"))
    rec("combo-successful-positive", all(float(r.get("pf") or 0) >= 1.1 and r.get("validated") for r in (blob.get("successful") or [])), blob.get("successful"))
    rec("combo-block-family", int(((blob.get("pfStats") or {}).get("block") or {}).get("n") or 0) == 12)
    rec("combo-dca-family", int(((blob.get("pfStats") or {}).get("dca") or {}).get("n") or 0) == 8)
    rec("combo-trailing-family", int(((blob.get("pfStats") or {}).get("trailing") or {}).get("n") or 0) == 10)
    rec("combo-engine", (blob.get("meta") or {}).get("engine") == "sqlite-memory", blob.get("meta"))
    rec("combo-matrix-cover", len(blob.get("matrix") or []) == len(INDICATIONS) * len(STRATEGIES), len(blob.get("matrix") or []))
    rec("combo-successful-have-identity", all(r.get("indication") and r.get("strategy") and r.get("config") for r in (blob.get("successful") or [])), blob.get("successful")[:3])
    rec("combo-family-ddt", all("maxDdS" in ((blob.get("pfStats") or {}).get(k) or {}) for k in PF_FAMILIES), blob.get("pfStats"))
    rec("combo-matrix-ddt", all("maxDdS" in (c or {}) for c in (blob.get("matrix") or [])[:5]), (blob.get("matrix") or [])[:1])
    rec("combo-successful-ddt", all("maxDdS" in r for r in (blob.get("successful") or [])) or not blob.get("successful"), blob.get("successful")[:1])
    return out


if __name__ == "__main__":
    rows = self_test()
    failed = [name for name, ok, _ in rows if not ok]
    print({"ok": not failed, "pass": len(rows) - len(failed), "fail": len(failed), "failed": failed})
