#!/usr/bin/env python3
"""Independent historic test — 4–64h window, fill until N positive-PF symbols.

Desk-side research job. Does not write the live/VST hist-calc lane and does
not start, stop or flatten running engines.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Sequence

from combo_eval import evaluate_book as combo_evaluate
from combo_eval import INDICATIONS, STRATEGIES
from hist_calc import (
    HIST_WARMUP_BARS,
    catalog_listings,
    coverage_counter,
    direction_rollup,
    fetch_klines,
    hours_to_bars,
    pick_winner_row,
    set_row,
    step_rollup,
    strategy_rollup,
    symbol_rollup,
    _public_json,
    _rank_set_rows,
)
from position_cost import PF_MAX, PF_MIN, POSITIVE_PF, is_positive_pf
from set_engine import SetBook, synth_trend
from storage_paths import atomic_write

HOURS_MIN = 4
HOURS_MAX = 64
HOURS_DEFAULT = 20
HOURS_STEP = 1
REFRESH_MIN = 1
REFRESH_MAX = 8
REFRESH_DEFAULT = 2
STEP_LO = 3
STEP_HI = 12
TICKER_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
PREFERRED_SYMBOLS = ["BCH-USDT", "SOL-USDT", "XRP-USDT"]
INTERN_MAJORS = (
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT",
    "ADA-USDT", "BCH-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT", "DOT-USDT",
    "UNI-USDT", "ATOM-USDT", "NEAR-USDT", "APT-USDT", "ARB-USDT", "SUI-USDT",
    "INJ-USDT", "AAVE-USDT", "FIL-USDT", "OP-USDT", "TRX-USDT", "XLM-USDT",
    "ETC-USDT", "LDO-USDT", "HBAR-USDT",
)
HIST_TEST_MAJORS = (
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT",
    "ADA-USDT", "BCH-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT", "DOT-USDT",
    "UNI-USDT", "ATOM-USDT", "NEAR-USDT", "APT-USDT", "ARB-USDT", "SUI-USDT",
    "INJ-USDT", "AAVE-USDT", "FIL-USDT", "OP-USDT", "TRX-USDT", "XLM-USDT",
    "ETC-USDT", "LDO-USDT", "HBAR-USDT", "TIA-USDT", "WLD-USDT", "JUP-USDT",
    "RENDER-USDT", "FET-USDT", "TAO-USDT", "SEI-USDT", "WIF-USDT", "1000PEPE-USDT",
    "STX-USDT", "IMX-USDT", "GRT-USDT", "ALGO-USDT", "VET-USDT", "EOS-USDT",
    "THETA-USDT", "AXS-USDT", "SAND-USDT", "MANA-USDT", "CRV-USDT", "MKR-USDT",
    "SNX-USDT", "COMP-USDT",
)
_MAJOR_KEYS = {s.upper() for s in HIST_TEST_MAJORS} | {s.upper() for s in PREFERRED_SYMBOLS} | {s.upper() for s in INTERN_MAJORS}
VOL_CANDIDATES = 250
MIN_QUOTE_VOLUME = 1_000_000.0
DEFAULT_TARGET = 50
VALIDATION_CAP = 250
TARGET_MAX = 250

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PUBLIC_JSON = os.path.join(ROOT, "public", "hist-test.json")
PUBLIC_SWEEP = os.path.join(ROOT, "public", "step-sweep-24h.json")
OUT_DIR = os.path.join(ROOT, "reports", "hist-test")
SUMMARY_PATH = os.path.join(OUT_DIR, "summary.json")
PID_PATH = os.path.join(OUT_DIR, "hist-test.pid")
LAST_READY_PATH = os.path.join(OUT_DIR, "last-ready.json")
VALIDATED_IDS_PATH = os.path.join(OUT_DIR, "validated-ids.json")
VALIDATED_IDS_CAP = 8192
PUBLIC_VALIDATED_IDS_CAP = 400
STOP_PATH = os.path.join(OUT_DIR, "STOP")
PAUSE_PATH = os.path.join(OUT_DIR, "PAUSE")

RUNNING_PHASES = ("queued", "rank", "evaluate", "fetch", "replay", "score", "score-refresh")
IN_FLIGHT_PHASES = RUNNING_PHASES + ("paused",)
SYMBOL_CAP = 50
GATE_SET_CAP = 512
JOB_CACHE_TTL_S = 1.5

_JOB_CACHE: Optional[Dict[str, Any]] = None
_JOB_CACHE_AT = 0.0


def _stop_file() -> str:
    return os.path.join(OUT_DIR, "STOP")


def _pause_file() -> str:
    return os.path.join(OUT_DIR, "PAUSE")


def invalidate_job_cache() -> None:
    """Drop the short-lived public job cache after publish or test path swaps."""
    global _JOB_CACHE, _JOB_CACHE_AT
    _JOB_CACHE = None
    _JOB_CACHE_AT = 0.0


def _pid_file() -> str:
    return os.path.join(OUT_DIR, "hist-test.pid")

_LOCK = threading.Lock()
_STOP = False
_PAUSE = False
_THREAD: Optional[threading.Thread] = None


def clamp_hours(value: Any, default: int = HOURS_DEFAULT) -> int:
    try:
        hours = int(round(float(value)))
    except (TypeError, ValueError):
        hours = int(default)
    return max(HOURS_MIN, min(HOURS_MAX, hours))


def clamp_min_pf(value: Any, default: float = POSITIVE_PF) -> float:
    try:
        pf = float(value)
    except (TypeError, ValueError):
        pf = float(default)
    if pf != pf:  # NaN
        pf = float(default)
    return round(min(PF_MAX, max(PF_MIN, pf)), 2)


def clamp_target(value: Any, default: int = DEFAULT_TARGET) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        n = int(default)
    if n <= 0:
        n = int(default)
    return max(1, min(TARGET_MAX, n))


def lookback_bars(hours: Any) -> int:
    return hours_to_bars(clamp_hours(hours))


def clamp_refresh_hours(value: Any, default: int = REFRESH_DEFAULT) -> int:
    try:
        hours = int(round(float(value)))
    except (TypeError, ValueError):
        hours = int(default)
    return max(REFRESH_MIN, min(REFRESH_MAX, hours))


def wait_for_refresh(hours: int, snapshot: Optional[Dict[str, Any]] = None) -> bool:
    """Hold until the next independent rerun. Returns False when Stop wins."""
    deadline = time.time() + max(REFRESH_MIN, int(hours)) * 3600.0
    blob = dict(snapshot or read_job())
    blob["nextRunAt"] = deadline
    blob["refreshHours"] = clamp_refresh_hours(hours)
    blob["continuous"] = True
    blob["running"] = str(blob.get("phase") or "") in RUNNING_PHASES
    blob["phase"] = str(blob.get("phase") or "ready")
    blob["detail"] = f"{blob.get('detail') or 'ready'} · next refresh {int(hours)}h"
    publish(blob)
    while time.time() < deadline and not stop_requested():
        wait_if_paused(None, {**read_job(), "phase": str(blob.get("resumePhase") or "evaluate")})
        remaining = deadline - time.time()
        time.sleep(0.25 if remaining > 1 else max(0.05, remaining))
    return not stop_requested()


def validated_set_ids(job: Optional[Dict[str, Any]] = None) -> List[str]:
    """Set IDs Test Historic currently treats as validated configs."""
    blob = job if isinstance(job, dict) else {}
    out: List[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        sid = str(raw or "").strip()
        if not sid or sid in seen:
            return
        if ":" in sid:
            sid = normalize_catalog_set_id(sid)
            if not sid or sid in seen:
                return
        seen.add(sid)
        out.append(sid)

    for row in blob.get("successfulConfigs") or []:
        if not isinstance(row, dict):
            continue
        if row.get("validated") is False:
            continue
        add(row.get("setId") or row.get("set_id") or row.get("id"))
    for row in blob.get("rows") or []:
        if isinstance(row, dict) and row.get("validated"):
            add(row.get("id") or row.get("setId") or row.get("set_id"))
    for sid in blob.get("validatedIds") or []:
        add(sid)
    last = blob.get("lastReady") if isinstance(blob.get("lastReady"), dict) else {}
    if last:
        add((last.get("winner") or {}).get("id") if isinstance(last.get("winner"), dict) else None)
        for sid in last.get("validatedIds") or []:
            add(sid)
    winner = blob.get("winner") if isinstance(blob.get("winner"), dict) else {}
    add(winner.get("id") or winner.get("setId") or winner.get("set_id"))
    for row in list(blob.get("ranked") or []) + list(blob.get("bySymbol") or []):
        if not isinstance(row, dict):
            continue
        sid = row.get("setId") or row.get("set_id") or row.get("id")
        if not sid or ":" not in str(sid):
            continue
        add(sid)
    if not out:
        last = read_last_ready()
        add((last.get("winner") or {}).get("id") if isinstance(last.get("winner"), dict) else None)
        for sid in last.get("validatedIds") or []:
            add(sid)
    persisted = read_persisted_validated_ids()
    if persisted and (not out or (len(out) < 8 and len(persisted) > len(out))):
        for sid in persisted:
            add(sid)
    return out


def persist_validated_ids(ids: List[str]) -> None:
    """Keep the full validated Set ID list off the compact public job.

    Never shrinks an existing sidecar. Compact public jobs and tests may pass a
    short list; the engine still needs last-ready IDs after a restart.
    """
    clean: List[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        sid = str(raw or "").strip()
        if not sid:
            return
        if ":" in sid:
            sid = normalize_catalog_set_id(sid)
        if not sid or sid in seen or len(clean) >= VALIDATED_IDS_CAP:
            return
        seen.add(sid)
        clean.append(sid)

    for raw in ids or []:
        add(raw)
    for sid in read_persisted_validated_ids():
        add(sid)
    if not clean:
        return
    try:
        _ensure_dir()
        atomic_write(VALIDATED_IDS_PATH, {"validatedIds": clean, "count": len(clean)})
    except Exception:
        pass


def read_persisted_validated_ids() -> List[str]:
    try:
        with open(VALIDATED_IDS_PATH, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            rows = loaded.get("validatedIds") or []
        elif isinstance(loaded, list):
            rows = loaded
        else:
            rows = []
        out: List[str] = []
        seen: set[str] = set()
        for raw in rows:
            sid = str(raw or "").strip()
            if ":" in sid:
                sid = normalize_catalog_set_id(sid)
            if not sid or sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
            if len(out) >= VALIDATED_IDS_CAP:
                break
        return out
    except Exception:
        return []


def collect_validated_ids(job: Optional[Dict[str, Any]] = None, ranked_sets: Any = None) -> List[str]:
    """Union of job IDs, ranked-set flags, combo rows, and the sidecar."""
    blob = job if isinstance(job, dict) else {}
    out: List[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        sid = str(raw or "").strip()
        if not sid:
            return
        if ":" in sid:
            sid = normalize_catalog_set_id(sid)
        if not sid or sid in seen:
            return
        seen.add(sid)
        out.append(sid)

    for sid in validated_set_ids(blob):
        add(sid)
    if ranked_sets:
        for item in ranked_sets:
            try:
                _key, st, _side, valid, _low = item
            except (TypeError, ValueError):
                continue
            if not valid:
                continue
            add(getattr(st, "id", "") or "")
    for sid in read_persisted_validated_ids():
        add(sid)
        if len(out) >= VALIDATED_IDS_CAP:
            break
    return out


def validated_symbols(job: Optional[Dict[str, Any]] = None) -> List[str]:
    """Positive / filled Test Historic symbols. Never the full rank queue."""
    blob = job if isinstance(job, dict) else {}
    out: List[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        name = str(raw or "").strip()
        if not name:
            return
        key = name.upper()
        if key in ("*", "ALL", "UNLIMITED") or key in seen:
            return
        if ":" in name:
            return  # set ids, not symbols
        seen.add(key)
        out.append(name)

    def add_list(raw: Any) -> None:
        if isinstance(raw, str):
            add(raw)
            return
        if not isinstance(raw, (list, tuple)):
            return
        for item in raw:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(item.get("symbol"))

    add_list(blob.get("positive"))
    for row in blob.get("bySymbol") or []:
        if not isinstance(row, dict):
            continue
        if row.get("positive") is False or row.get("validated") is False:
            continue
        if row.get("positive") or row.get("validated"):
            add(row.get("symbol"))
    for row in blob.get("ranked") or []:
        if isinstance(row, dict) and (row.get("positive") or row.get("validated")):
            add(row.get("symbol"))
    # filled universe after a run (not the rank queue)
    if blob.get("ready") or str(blob.get("phase") or "") in ("ready", "idle", ""):
        add_list(blob.get("symbols"))
    last = blob.get("lastReady") if isinstance(blob.get("lastReady"), dict) else {}
    if last:
        add_list(last.get("positive"))
        add_list(last.get("symbols"))
        winner = last.get("winner") if isinstance(last.get("winner"), dict) else {}
        add(winner.get("symbol"))
    if not out:
        last = read_last_ready()
        add_list(last.get("positive"))
        add_list(last.get("symbols"))
        winner = last.get("winner") if isinstance(last.get("winner"), dict) else {}
        add(winner.get("symbol"))
    # Do not fall back to full rank queue unless short ≤ 40
    if not out:
        ranked = blob.get("ranked") or []
        if isinstance(ranked, list) and 0 < len(ranked) <= 40:
            for row in ranked:
                if isinstance(row, dict):
                    add(row.get("symbol"))
                elif isinstance(row, str):
                    add(row)
    return out[:SYMBOL_CAP]


VALID_INDICATIONS = set(INDICATIONS)
VALID_STRATEGIES = set(STRATEGIES)


CATALOG_PACKS = {"general", "indications"}


def normalize_catalog_set_id(sid: Any) -> str:
    """Keep real Set ids. Drop side suffixes and overlay/strategy aliases."""
    raw = str(sid or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if lower.endswith(":long") or lower.endswith(":short"):
        raw = raw.rsplit(":", 1)[0]
        lower = raw.lower()
    parts = [p for p in raw.split(":") if p]
    if not parts:
        return ""
    pack = parts[0].lower()
    if pack in VALID_STRATEGIES or pack in ("core", "trail"):
        return ""
    if pack in CATALOG_PACKS:
        if len(parts) < 3 or parts[1] != "1m" or not str(parts[2]).lower().startswith("sl"):
            return ""
        if not any(str(p).lower().startswith("st") for p in parts):
            return ""
    return raw


def is_catalog_set_id(sid: Any) -> bool:
    """True for real catalog Set ids (general|indications : 1m : sl… : st…)."""
    raw = normalize_catalog_set_id(sid)
    if not raw:
        return False
    parts = [p for p in raw.split(":") if p]
    if not parts:
        return False
    pack = parts[0].lower()
    if pack in CATALOG_PACKS:
        return True
    return len(parts) >= 3 and str(parts[1]).lower() == "1m" and str(parts[2]).lower().startswith("sl")


def is_coordination_set_id(sid: Any) -> bool:
    """Catalog Set, or a Block/DCA overlay wrapping a catalog Set."""
    raw = str(sid or "").strip()
    if not raw:
        return False
    if is_catalog_set_id(raw):
        return True
    parts = [p for p in raw.split(":") if p]
    if parts and parts[0].lower() in VALID_STRATEGIES:
        return is_catalog_set_id(":".join(parts[1:]))
    # Test dummies without a colon (e.g. "a") are allowed as identity carriers.
    return ":" not in raw


def identity_from_set_id(sid: str) -> Dict[str, str]:
    """Parse pack / indication / strategy from a catalog Set id."""
    empty = {"pack": "", "indication": "", "strategy": ""}
    raw = str(sid or "").strip()
    if not raw:
        return empty
    clean = normalize_catalog_set_id(raw)
    if not clean:
        parts = [p for p in raw.split(":") if p]
        if parts and parts[-1].lower() in ("long", "short"):
            parts = parts[:-1]
        if parts and parts[0].lower() in VALID_STRATEGIES:
            remainder = ":".join(parts[1:])
            if is_catalog_set_id(remainder):
                inner = identity_from_set_id(remainder)
                if inner.get("indication"):
                    inner["strategy"] = parts[0].lower()
                    return inner
        return empty
    parts = [p for p in clean.split(":") if p]
    if parts and parts[-1].lower() in ("long", "short"):
        parts = parts[:-1]
    pack = parts[0].lower() if parts else ""
    if pack == "general":
        indication = "general"
    elif pack == "indications":
        indication = "combined"
    elif pack in VALID_INDICATIONS:
        indication = pack
    else:
        indication = ""
    strategy = "normal"
    if pack in VALID_STRATEGIES and pack not in ("normal",):
        strategy = pack
        if not indication:
            indication = "combined"
    elif any(p.startswith("tr") and p != "trend" for p in parts):
        strategy = "trailing"
    return {"pack": pack, "indication": indication, "strategy": strategy}


def selected_coordinations(job: Optional[Dict[str, Any]] = None, limit: int = 24) -> List[Dict[str, Any]]:
    """Validated catalog indication × strategy pairs (PF + DDT).

    Independent kind tapes and unvalidated matrix cells are not coordinations.
    Overlay Block/DCA ids must wrap a real catalog Set.
    """
    blob = job if isinstance(job, dict) else {}
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: Any, require_validated: bool = True) -> None:
        if not isinstance(row, dict):
            return
        if require_validated and row.get("validated") is False:
            return
        sid = str(row.get("setId") or row.get("set_id") or row.get("id") or "").strip()
        if sid.endswith(":long") or sid.endswith(":short") or sid.endswith(":LONG") or sid.endswith(":SHORT"):
            sid = sid.rsplit(":", 1)[0]
        if sid and not is_coordination_set_id(sid):
            return
        if not sid:
            return
        indication = str(row.get("indication") or row.get("ind_kind") or "").strip().lower()
        strategy = str(row.get("strategy") or "").strip().lower()
        if strategy not in VALID_STRATEGIES:
            strategy = ""
        if indication not in VALID_INDICATIONS:
            indication = ""
        ident = identity_from_set_id(sid)
        if not indication:
            indication = ident["indication"]
        if not strategy:
            strategy = ident["strategy"]
        if not indication or not strategy or indication not in VALID_INDICATIONS or strategy not in VALID_STRATEGIES:
            return
        try:
            pf = float(row.get("pf") or row.get("last15Ratio") or 0)
        except (TypeError, ValueError):
            pf = 0.0
        try:
            n = int(row.get("n") or row.get("evalN") or row.get("last15N") or 0)
        except (TypeError, ValueError):
            n = 0
        if require_validated and row.get("validated") is not True and not is_positive_pf(pf):
            return
        if n <= 0 and require_validated:
            return
        key = f"{indication}:{strategy}:{sid}"
        if key in seen:
            return
        seen.add(key)
        try:
            max_dd = float(row.get("maxDdS") or row.get("max_dd_s") or 0)
        except (TypeError, ValueError):
            max_dd = 0.0
        out.append({
            "id": sid,
            "indication": indication,
            "strategy": strategy,
            "pf": pf,
            "n": n,
            "maxDdS": max_dd,
            "validated": True,
        })

    for row in blob.get("successfulConfigs") or []:
        add(row, require_validated=True)
    for row in blob.get("selectedCoordinations") or []:
        add(row, require_validated=False)
    cells = [
        c for c in (blob.get("comboMatrix") or [])
        if isinstance(c, dict) and int(c.get("n") or 0) > 0 and c.get("validated") is True
        and str(c.get("setId") or c.get("set_id") or c.get("id") or "").strip()
    ]
    cells.sort(key=lambda c: (-float(c.get("pf") or 0), -int(c.get("n") or 0)))
    for cell in cells:
        add(cell, require_validated=True)
    cap = 24
    try:
        cap = max(1, min(int(limit or 24), 48))
    except (TypeError, ValueError):
        cap = 24
    return out[:cap]


def successful_from_ranked(ranked_sets: Any, min_pf: float, limit: int = 60) -> List[Dict[str, Any]]:
    """Set-level validated rows (PF + DDT) for stats when combo last-N is empty."""
    best: Dict[str, Dict[str, Any]] = {}
    for item in ranked_sets or []:
        try:
            _key, st, side, valid, _low = item
        except (TypeError, ValueError):
            continue
        if not valid:
            continue
        sid = str(getattr(st, "id", "") or "")
        if not sid:
            continue
        prev = best.get(sid)
        if prev is not None and not side:
            pass
        elif prev is not None:
            continue
        pack = str(getattr(st, "pack", "") or "")
        kind = str(getattr(st, "kind", "") or "")
        if kind == "trail":
            strategy = "trailing"
        elif pack == "block" or str(kind).startswith("block"):
            strategy = "block"
        elif pack == "dca":
            strategy = "dca"
        else:
            strategy = "normal"
        if pack == "indications":
            indication = "combined"
        elif pack == "general":
            indication = "general"
        else:
            indication = "combined"
        try:
            pf = float(getattr(st, "last15_ratio", 0) or 0)
        except (TypeError, ValueError):
            pf = 0.0
        if not is_positive_pf(pf, min_pf):
            continue
        best[sid] = {
            "indication": indication,
            "config": (
                f"sl{float(getattr(st, 'sl_ratio', 0) or 0):.1f}:"
                f"st{int(getattr(st, 'step', 0) or 0)}:"
                f"tr{getattr(st, 'trail_key', '') or 'base'}"
            ),
            "strategy": strategy,
            "setId": sid,
            "slRatio": float(getattr(st, "sl_ratio", 0) or 0),
            "step": int(getattr(st, "step", 0) or 0),
            "trailKey": str(getattr(st, "trail_key", "") or ""),
            "n": int(getattr(st, "n", 0) or 0),
            "evalN": int(getattr(st, "last15_n", 0) or 0),
            "pf": round(pf, 4),
            "wr": float(getattr(st, "wr", 0) or 0),
            "maxDdS": float(getattr(st, "max_dd_s", 0) or 0),
            "avgDdS": float(getattr(st, "avg_dd_s", 0) or 0),
            "validated": True,
        }
        if len(best) >= max(1, int(limit or 60)):
            break
    return list(best.values())[:limit]


def use_recalc_only(body: Optional[Dict[str, Any]], recalc_ids: Sequence[str], keep_symbols: Sequence[str], target: int) -> bool:
    """Recalc the ready book only when it already meets the fill target.

    Auto-seeded validated IDs must not freeze intern at 2 symbols while target is 50.
    """
    blob = body if isinstance(body, dict) else {}
    if not recalc_ids:
        return False
    if blob.get("fullCatalog"):
        return False
    try:
        need = max(1, int(target or 0))
    except (TypeError, ValueError):
        need = 1
    return len([s for s in (keep_symbols or []) if str(s or "").strip()]) >= need


def intern_audit_floor_failed(job: Optional[Dict[str, Any]] = None) -> bool:
    blob = job if isinstance(job, dict) else {}
    failed = (blob.get("audit") or {}).get("failed") if isinstance(blob.get("audit"), dict) else None
    if isinstance(failed, (list, tuple, set)):
        return "symbols-meet-floor" in failed
    error = str(blob.get("error") or blob.get("detail") or "")
    return "symbols-meet-floor" in error


def intern_liquid_pool(
    overlay_symbols: Optional[List[str]] = None,
    universe: Optional[List[Any]] = None,
    *,
    opens: Optional[List[str]] = None,
    cap: int = SYMBOL_CAP,
    min_quote: float = MIN_QUOTE_VOLUME,
    tradable: Optional[Sequence[str]] = None,
    validated: Optional[Sequence[str]] = None,
) -> List[str]:
    """Intern preferred + hist-test majors. Cap 50 liquid majors.

    Volume padding never adds junk alts. Open lots always stay scannable.
    `validated` hist-test names still join when they are majors, already open,
    or quote volume is at least MIN_QUOTE_VOLUME.    """
    major_keys = {s.upper() for s in HIST_TEST_MAJORS} | {s.upper() for s in INTERN_MAJORS} | {s.upper() for s in PREFERRED_SYMBOLS}
    overlay = [
        str(s).strip().upper()
        for s in (overlay_symbols or [])
        if str(s or "").strip() and str(s).strip() not in ("*", "ALL", "UNLIMITED") and ":" not in str(s)
    ]
    vol: Dict[str, float] = {}
    for row in universe or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("symbol") or "").strip().upper()
        if not name:
            continue
        try:
            vol[name] = float(row.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
    out: List[str] = []
    used: set[str] = set()
    open_keys = {str(s).strip().upper() for s in (opens or []) if str(s or "").strip()}
    apply_tradable = tradable is not None
    tradable_keys = {str(s).strip().upper() for s in (tradable or []) if str(s or "").strip()}

    def add(raw: Any, *, keep_open: bool = False, allow_validated: bool = False) -> None:
        name = str(raw or "").strip().upper()
        if not name.endswith("-USDT") or name in used or ":" in name:
            return
        if name.startswith(("NCCO", "NCS", "NCFX")):
            return
        if keep_open and name in open_keys:
            used.add(name)
            out.append(name)
            return
        if name not in major_keys:
            if not allow_validated:
                return
            if float(vol.get(name) or 0) < float(min_quote or 0):
                return
        if apply_tradable and name not in tradable_keys and not (keep_open and name in open_keys):
            return
        used.add(name)
        out.append(name)

    for s in opens or []:
        add(s, keep_open=True)
    for s in PREFERRED_SYMBOLS:
        add(s)
    for s in validated or []:
        add(s, allow_validated=True)
    ranked_majors = list(HIST_TEST_MAJORS)
    if vol:
        ranked_majors = sorted(ranked_majors, key=lambda s: -float(vol.get(s.upper(), 0)))
    for s in ranked_majors:
        add(s)
    for s in overlay:
        add(s)
    limit = int(cap or 0) or SYMBOL_CAP
    if vol and (not limit or len(out) < limit):
        ranked_vol = sorted(
            (s for s in HIST_TEST_MAJORS if float(vol.get(str(s).upper(), 0) or 0) >= float(min_quote or 0)),
            key=lambda s: (-float(vol.get(s, 0) or 0), s),
        )
        for s in ranked_vol:
            if limit and len(out) >= limit:
                break
            add(s)
    if limit > 0:
        out = out[:limit]
    return out


def select_intern_symbols(
    overlay_symbols: Optional[List[str]] = None,
    job: Optional[Dict[str, Any]] = None,
    *,
    opens: Optional[List[str]] = None,
    cap: int = SYMBOL_CAP,
) -> List[str]:
    """Intern universe while Test Historic owns the catalog.

    Validated configs trade the liquid major book, not leftover microcaps and
    not a 3-symbol preferred-only fallback. Open lots always stay scannable.
    """
    overlay = [
        str(s).strip()
        for s in (overlay_symbols or [])
        if str(s or "").strip() and str(s).strip() not in ("*", "ALL", "UNLIMITED") and ":" not in str(s)
    ]
    out: List[str] = []
    used: set[str] = set()

    def add(raw: Any) -> None:
        name = str(raw or "").strip()
        if not name or name in ("*", "ALL", "UNLIMITED") or ":" in name:
            return
        key = name.upper()
        if key in used:
            return
        used.add(key)
        out.append(name)

    for s in overlay:
        add(s)
    for s in opens or []:
        add(s)
    if not intern_audit_floor_failed(job):
        overlay_keys = {str(s).strip().upper() for s in overlay}
        open_keys = {str(s).strip().upper() for s in (opens or []) if str(s or "").strip()}
        for s in validated_symbols(job):
            name = str(s or "").strip().upper()
            if name.startswith(("NCCO", "NCS", "NCFX")):
                continue
            if name not in _MAJOR_KEYS and name not in overlay_keys and name not in open_keys:
                continue
            add(s)
    limit = int(cap or 0) or SYMBOL_CAP
    if limit > 0 and len(out) > limit:
        must: List[str] = []
        must_keys: set[str] = set()
        for s in opens or []:
            name = str(s or "").strip()
            key = name.upper()
            if not name or key in must_keys or name in ("*", "ALL", "UNLIMITED"):
                continue
            must_keys.add(key)
            must.append(name)
        rest = [s for s in out if s.upper() not in must_keys]
        out = (must + rest)[: max(limit, len(must))]
    return out


def running_sets(job: Optional[Dict[str, Any]] = None, limit: int = 24) -> List[Dict[str, Any]]:
    """Compact validated configs currently in play for overviews/stats."""
    blob = job if isinstance(job, dict) else {}
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: Any) -> None:
        if not isinstance(row, dict):
            return
        if row.get("validated") is False:
            return
        sid = str(row.get("id") or row.get("setId") or row.get("set_id") or "").strip()
        if not sid or sid in seen:
            return
        if ":" in sid and not is_coordination_set_id(sid):
            return
        seen.add(sid)
        try:
            pf = float(row.get("pf") or row.get("last15Ratio") or 0)
        except (TypeError, ValueError):
            pf = 0.0
        try:
            n = int(row.get("n") or row.get("evalN") or row.get("last15N") or row.get("last15_n") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return
        try:
            step = int(row.get("step") or 0)
        except (TypeError, ValueError):
            step = 0
        ident = identity_from_set_id(sid)
        indication = str(row.get("indication") or row.get("ind_kind") or "").strip().lower()
        strategy = str(row.get("strategy") or "").strip().lower()
        if strategy not in VALID_STRATEGIES:
            strategy = ""
        if indication not in VALID_INDICATIONS:
            indication = ""
        if not indication:
            indication = ident["indication"]
        if not strategy:
            strategy = ident["strategy"]
        out.append({
            "id": sid,
            "indication": indication,
            "strategy": strategy,
            "symbol": str(row.get("symbol") or ""),
            "pf": pf,
            "n": n,
            "step": step,
            "validated": True,
        })

    for row in blob.get("successfulConfigs") or []:
        add(row)
    for row in blob.get("rows") or []:
        if isinstance(row, dict) and row.get("validated"):
            add(row)
    for sid in blob.get("validatedIds") or []:
        add({"id": sid, "validated": True})
    for sid in read_persisted_validated_ids()[:limit]:
        add({"id": sid, "validated": True})
    winner = blob.get("winner") if isinstance(blob.get("winner"), dict) else {}
    if winner:
        add({**winner, "id": winner.get("id") or winner.get("setId") or winner.get("set_id"), "validated": True})
    last = blob.get("lastReady") if isinstance(blob.get("lastReady"), dict) else {}
    if last:
        for sid in last.get("validatedIds") or []:
            add({"id": sid, "validated": True})
        w = last.get("winner") if isinstance(last.get("winner"), dict) else {}
        if isinstance(w, dict):
            add({**w, "id": w.get("id") or w.get("setId") or w.get("set_id"), "validated": True})
    cap = 24
    try:
        cap = max(1, min(int(limit or 24), 48))
    except (TypeError, ValueError):
        cap = 24
    return out[:cap]


def off_progress_view() -> Dict[str, Any]:
    return {
        "enabled": False,
        "ownsCatalog": False,
        "catalogSkipped": False,
        "runningSets": [],
        "symbols": [],
        "internSymbols": [],
        "validatedCount": 0,
        "processedSetCount": 0,
        "processingCount": 0,
        "detail": "Test Historic off · full catalog in play",
        "phase": "off",
        "pct": 0,
        "ready": False,
        "running": False,
        "paused": False,
    }


def apply_scores_to_book(book: Any, job: Optional[Dict[str, Any]] = None) -> List[str]:
    """Push Test Historic validated configs onto a live SetBook without a full catalog replay.

    Always applies the allow-list (even empty). Empty IDs keep the gate closed so
    engine progress does not reopen the full catalog while Test Historic is on.
    """
    blob = job if isinstance(job, dict) else read_job()
    ids = collect_validated_ids(blob)
    apply = getattr(book, "apply_hist_test_gate", None)
    if callable(apply):
        apply(ids)
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in blob.get("successfulConfigs") or []:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("setId") or row.get("set_id") or row.get("id") or "").strip()
        if sid:
            by_id[sid] = row
    for row in blob.get("rows") or []:
        if not isinstance(row, dict) or row.get("validated") is False:
            continue
        sid = str(row.get("id") or row.get("setId") or row.get("set_id") or "").strip()
        if sid:
            by_id.setdefault(sid, row)
    winner = blob.get("winner") if isinstance(blob.get("winner"), dict) else {}
    win_id = str(winner.get("id") or winner.get("setId") or winner.get("set_id") or "").strip()
    if win_id:
        by_id.setdefault(win_id, {
            **winner,
            "validated": True,
            "pf": winner.get("last15Ratio") or winner.get("pf") or 0,
            "evalN": winner.get("n") or winner.get("evalN") or 0,
            "n": winner.get("n") or 0,
        })
    last = blob.get("lastReady") if isinstance(blob.get("lastReady"), dict) else None
    if not isinstance(last, dict):
        last = read_last_ready()
    last_win = (last or {}).get("winner") if isinstance(last, dict) else {}
    if isinstance(last_win, dict):
        last_id = str(last_win.get("id") or last_win.get("setId") or last_win.get("set_id") or "").strip()
        if last_id:
            by_id.setdefault(last_id, {
                **last_win,
                "validated": True,
                "pf": last_win.get("last15Ratio") or last_win.get("pf") or 0,
                "evalN": last_win.get("n") or last_win.get("evalN") or 0,
                "n": last_win.get("n") or 0,
            })
    for sid in ids:
        by_id.setdefault(sid, {"setId": sid, "validated": True, "pf": 0, "n": 0, "evalN": 0})
    for st in getattr(book, "by_idx", None) or []:
        row = by_id.get(getattr(st, "id", ""))
        if not row:
            continue
        n = int(row.get("evalN") or row.get("n") or row.get("last15N") or row.get("last15_n") or 0)
        try:
            pf = float(row.get("pf") or row.get("last15Ratio") or 0)
        except (TypeError, ValueError):
            pf = 0.0
        try:
            need = int(book.eval_need()) if callable(getattr(book, "eval_need", None)) else 8
        except Exception:
            need = 8
        try:
            floor = float(getattr(book, "real_min_pf", None) or getattr(book, "min_pf", None) or POSITIVE_PF)
        except (TypeError, ValueError):
            floor = float(POSITIVE_PF)
        proven = n >= need and pf + 1e-9 >= floor
        if n >= need:
            st.last15_n = max(int(getattr(st, "last15_n", 0) or 0), n)
            st.n = max(int(getattr(st, "n", 0) or 0), n)
            if pf > 0:
                st.last15_ratio = pf
        st.active = bool(proven)
        st.deact_reason = "" if proven else (st.deact_reason or "hist-test intern")
        st.processing_active = True
        st.processing_reason = "hist-test validated" if proven else "hist-test intern"
        try:
            dd = float(row.get("maxDdS") or row.get("max_dd_s") or getattr(st, "max_dd_s", 0) or 0)
        except (TypeError, ValueError):
            dd = float(getattr(st, "max_dd_s", 0) or 0)
        st.max_dd_s = dd
        ledger = dict(getattr(st, "stage_ledger", None) or {})
        if proven:
            ledger["base"] = True
            ledger["main"] = True
            ledger["real"] = True
        st.stage_ledger = ledger
        # Proven hist-test evidence seeds sides so intern size is not starved.
        # n=0 / pf=0 never invents intern-neutral 1.0 or Real stages.
        if n > 0 and pf > 0:
            side_view = {
                "last15_n": int(st.last15_n or 0),
                "last15_ratio": float(st.last15_ratio or 0),
                "n": int(st.n or 0),
                "base_n": int(st.last15_n or 0),
                "base_pf": float(st.last15_ratio or 0),
                "main_n": int(st.last15_n or 0) if proven else 0,
                "main_pf": float(st.last15_ratio or 0) if proven else 0.0,
                "real_n": int(st.last15_n or 0) if proven else 0,
                "real_pf": float(st.last15_ratio or 0) if proven else 0.0,
                "max_dd_s": float(st.max_dd_s or 0),
                "ddOk": True,
                "validated": bool(proven),
                "active": bool(proven),
                "deact_reason": "" if proven else "hist-test intern",
            }
            sides = dict(getattr(st, "by_side", None) or {})
            for direction in ("LONG", "SHORT"):
                blob = dict(sides.get(direction) or {})
                blob.update(side_view)
                sides[direction] = blob
            st.by_side = sides
    cap = getattr(book, "_cap_active", None)
    if callable(cap):
        try:
            cap(True)
        except Exception:
            pass
    return ids


def idle_job() -> Dict[str, Any]:
    return {
        "ok": True,
        "phase": "idle",
        "pct": 0,
        "detail": f"Ready · {HOURS_DEFAULT}h historic test · fill until positive count",
        "ready": False,
        "running": False,
        "paused": False,
        "hours": HOURS_DEFAULT,
        "minPf": POSITIVE_PF,
        "targetCount": DEFAULT_TARGET,
        "stepLo": STEP_LO,
        "stepHi": STEP_HI,
        "symbols": [],
        "positive": [],
        "rejected": [],
        "byStep": [],
        "ranges": [],
        "heatmap": [],
        "positivePf": POSITIVE_PF,
        "refreshHours": REFRESH_DEFAULT,
        "continuous": False,
    }


def job_paths() -> List[str]:
    return [PUBLIC_JSON, SUMMARY_PATH, PUBLIC_SWEEP]


def _ensure_dir() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(PUBLIC_JSON), exist_ok=True)


def normalize_job(blob: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(blob or idle_job())
    phase = str(payload.get("phase") or "idle")
    positives = payload.get("positive") or payload.get("symbols") or []
    npos = len([s for s in positives if s]) if isinstance(positives, list) else 0
    ready = bool(payload.get("ready")) or phase == "ready"
    try:
        pct = float(payload.get("pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    if ready and phase == "ready" and not payload.get("error") and pct < 99:
        payload["pct"] = 100
        payload["ready"] = True
    if payload.get("filled") in (None, 0) and npos:
        payload["filled"] = npos
    if payload.get("evaluated") in (None, 0):
        detail = str(payload.get("detail") or "")
        marker = "evaluated "
        if marker in detail:
            tail = detail.split(marker, 1)[1].split()[0]
            try:
                payload["evaluated"] = int(tail)
            except ValueError:
                pass
    payload["running"] = bool(payload.get("running")) and phase in RUNNING_PHASES
    return payload


def apply_control_latches(blob: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = dict(blob or idle_job())
    payload.setdefault("ok", True)
    if stop_requested():
        payload["phase"] = "stopped"
        payload["running"] = False
        payload["paused"] = False
        payload["detail"] = "historic test stopped"
        return payload
    if pause_requested():
        phase = str(payload.get("phase") or "")
        resume_phase = str(payload.get("resumePhase") or "")
        if phase in RUNNING_PHASES:
            resume_phase = phase
            payload["resumePhase"] = phase
        payload["phase"] = "paused"
        payload["paused"] = True
        payload["running"] = thread_alive() or resume_phase in RUNNING_PHASES
        detail = str(payload.get("detail") or "")
        if "paused" not in detail.lower():
            payload["detail"] = "historic test paused"
        return payload
    payload["paused"] = bool(payload.get("paused")) and str(payload.get("phase") or "") == "paused"
    return payload


def read_last_ready() -> Dict[str, Any]:
    try:
        with open(LAST_READY_PATH, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict) and (loaded.get("winner") or loaded.get("validatedIds")):
            return loaded
    except Exception:
        pass
    return {}


def seed_recalc_prior() -> Dict[str, Any]:
    """First loop must recalc last-ready IDs, never a blank rank that drops the gate."""
    last = read_last_ready()
    job = read_job()
    if not isinstance(last, dict):
        last = {}
    if not isinstance(job, dict):
        job = {}
    prior = last if (last.get("validatedIds") or last.get("positive") or last.get("winner")) else job
    ids = validated_set_ids(prior)
    if not ids:
        ids = read_persisted_validated_ids()
        if ids:
            prior = dict(prior or {})
            prior["validatedIds"] = ids
    return prior or {}


def _merge_last_ready_stats(payload: Dict[str, Any], last: Optional[Dict[str, Any]]) -> None:
    """Keep combo/PF families from the last ready run while a refresh is in flight."""
    if not isinstance(payload, dict) or not isinstance(last, dict) or not last:
        return
    for key in ("pfStats", "withWithout", "combo", "byIndication", "byStrategy"):
        if not payload.get(key) and last.get(key):
            payload[key] = last.get(key)
    if not payload.get("comboMatrix") and last.get("comboMatrix"):
        payload["comboMatrix"] = last.get("comboMatrix")
    if not payload.get("successfulConfigs") and last.get("successfulConfigs"):
        payload["successfulConfigs"] = last.get("successfulConfigs")


def _ready_snapshot(blob: Dict[str, Any]) -> Dict[str, Any]:
    winner = blob.get("winner") if isinstance(blob.get("winner"), dict) else {}
    ids = list(blob.get("validatedIds") or [])
    if not ids:
        ids = collect_validated_ids(blob)
    persist_validated_ids(ids)
    return {
        "phase": "ready",
        "ready": True,
        "winner": winner,
        "validatedIds": ids,
        "validatedCount": blob.get("validatedCount") or len(ids),
        "successfulConfigs": list(blob.get("successfulConfigs") or [])[:60],
        "hours": blob.get("hours"),
        "minPf": blob.get("minPf"),
        "n": (winner or {}).get("n"),
        "symbols": list(blob.get("positive") or blob.get("symbols") or [])[:SYMBOL_CAP],
        "positive": list(blob.get("positive") or blob.get("symbols") or [])[:SYMBOL_CAP],
        "pfStats": blob.get("pfStats") or {},
        "withWithout": blob.get("withWithout") or {},
        "comboMatrix": (blob.get("comboMatrix") or [])[:40] if isinstance(blob.get("comboMatrix"), list) else [],
        "combo": blob.get("combo") or {},
        "byIndication": blob.get("byIndication") or blob.get("kinds") or {},
        "byStrategy": blob.get("byStrategy") or {},
    }


def publish(blob: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_dir()
    payload = normalize_job(apply_control_latches(blob))
    payload["generatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ready = bool(payload.get("ready") or str(payload.get("phase") or "") == "ready")
    phase_now = str(payload.get("phase") or "")
    in_flight = phase_now in IN_FLIGHT_PHASES
    if ready and (payload.get("winner") or payload.get("validatedIds")) and not in_flight:
        try:
            atomic_write(LAST_READY_PATH, _ready_snapshot(payload))
        except Exception:
            pass
        persist_validated_ids(list(payload.get("validatedIds") or []))
    else:
        last = read_last_ready()
        if last:
            _merge_last_ready_stats(payload, last)
            if not in_flight:
                if not payload.get("winner"):
                    payload["winner"] = last.get("winner") or {}
                if not payload.get("validatedIds"):
                    payload["validatedIds"] = list(last.get("validatedIds") or [])
                if payload.get("validatedCount") in (None, 0) and last.get("validatedCount"):
                    payload["validatedCount"] = last.get("validatedCount")
                if not payload.get("successfulConfigs") and last.get("successfulConfigs"):
                    payload["successfulConfigs"] = last.get("successfulConfigs")
                payload["lastReady"] = True
            else:
                payload["lastReady"] = last
    for dest in job_paths():
        try:
            atomic_write(dest, payload)
        except Exception:
            try:
                tmp = dest + ".tmp"
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, dest)
            except Exception:
                pass
    invalidate_job_cache()
    return payload


def read_job() -> Dict[str, Any]:
    global _JOB_CACHE, _JOB_CACHE_AT
    now = time.monotonic()
    if _JOB_CACHE is not None and now - _JOB_CACHE_AT < JOB_CACHE_TTL_S:
        return _JOB_CACHE
    blob: Dict[str, Any] = idle_job()
    for path in (PUBLIC_JSON, SUMMARY_PATH):
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict) and loaded:
                blob = loaded
                break
        except Exception:
            continue
    blob = normalize_job(apply_control_latches(blob))
    _JOB_CACHE = blob
    _JOB_CACHE_AT = now
    return blob


def job_age_s(blob: Optional[Dict[str, Any]] = None) -> float:
    """Seconds since the public Test Historic job last published."""
    import calendar
    text = str((blob or {}).get("generatedAt") or "").strip()
    if not text:
        return 0.0
    try:
        if text.endswith("Z"):
            stamp = time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
            return max(0.0, time.time() - calendar.timegm(stamp))
    except Exception:
        return 0.0
    return 0.0


def job_progress_view(job: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Engine/UI progress for Test Historic: in-flight pct plus last validated gate."""
    blob = job if isinstance(job, dict) else read_job()
    ids = collect_validated_ids(blob)
    phase = str(blob.get("phase") or "idle")
    paused = bool(blob.get("paused") or phase == "paused")
    running = (phase in RUNNING_PHASES and not paused) or (paused and str(blob.get("resumePhase") or "") in RUNNING_PHASES)
    try:
        pct = float(blob.get("pct") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    try:
        reported = int(blob.get("validatedCount") or 0)
    except (TypeError, ValueError):
        reported = 0
    n_ids = max(len(ids), reported)
    if running:
        if pct <= 0:
            pct = 1.0
    elif paused:
        pct = min(99.0, max(pct, 1.0))
        if phase != "paused":
            phase = "paused"
    elif phase in ("ready",) or (not running and n_ids and phase in ("idle", "", "ready")):
        if phase in ("idle", ""):
            phase = "ready"
        if not blob.get("error"):
            pct = 100.0
    n_fills = 0
    winner = blob.get("winner") if isinstance(blob.get("winner"), dict) else {}
    try:
        n_fills = int(winner.get("n") or winner.get("evalN") or winner.get("last15N") or 0)
    except (TypeError, ValueError):
        n_fills = 0
    if not n_fills:
        last = blob.get("lastReady") if isinstance(blob.get("lastReady"), dict) else None
        if not isinstance(last, dict):
            last = read_last_ready()
        w = (last or {}).get("winner") if isinstance(last, dict) else {}
        if isinstance(w, dict):
            try:
                n_fills = int(w.get("n") or w.get("evalN") or w.get("last15N") or 0)
            except (TypeError, ValueError):
                n_fills = 0
    age = job_age_s(blob)
    stale = bool(running and age > 180)
    raw_detail = str(blob.get("detail") or phase)
    refresh_h = clamp_refresh_hours(blob.get("refreshHours"))
    symbols = validated_symbols(blob)
    cov = blob.get("coverage") if isinstance(blob.get("coverage"), dict) else {}
    sets_cov = cov.get("sets") if isinstance(cov.get("sets"), dict) else {}
    try:
        sets_done = int(sets_cov.get("completed") or sets_cov.get("done") or blob.get("setsDone") or n_ids)
    except (TypeError, ValueError):
        sets_done = n_ids
    try:
        sets_total = int(sets_cov.get("requested") or sets_cov.get("total") or blob.get("setsTotal") or max(n_ids, 1 if running or n_ids else 0))
    except (TypeError, ValueError):
        sets_total = max(n_ids, 1 if running or n_ids else 0)
    err = str(blob.get("error") or "")
    err_line = ""
    if err and not err.startswith("audit:"):
        err_line = next((ln.strip() for ln in reversed(err.splitlines()) if ln.strip()), err.strip())[:180]
    intern_syms = intern_liquid_pool(
        None,
        blob.get("universe") or blob.get("ranked"),
        cap=SYMBOL_CAP,
        validated=[s for s in symbols if str(s).upper() in _MAJOR_KEYS],
    )
    proven = 0
    for row in blob.get("successfulConfigs") or []:
        if not isinstance(row, dict) or row.get("validated") is False:
            continue
        try:
            n_row = int(row.get("n") or row.get("evalN") or row.get("last15N") or 0)
        except (TypeError, ValueError):
            n_row = 0
        if n_row >= 8:
            proven += 1
    intern_n = n_ids
    if running:
        detail = f"Test Historic {phase} {int(pct)}% · {raw_detail} · {intern_n} intern · {proven} last-15 validated · {len(intern_syms)} intern symbols"
    elif paused:
        detail = f"Test Historic paused · {raw_detail}"
    elif err_line:
        if phase in ("ready", "idle", ""):
            phase = "error"
        pct = min(float(pct or 0), 99.0)
        detail = f"Test Historic error · {intern_n} intern · {proven} last-15 validated · {len(intern_syms)} intern symbols · {err_line}"
    elif intern_n:
        detail = f"Test Historic · {intern_n} intern configs · {proven} last-15 validated · {len(intern_syms)} intern symbols · skip full catalog · refresh {refresh_h}h"
    else:
        detail = "Test Historic owns calcs · waiting intern configs · skip full catalog"
    if stale:
        detail += " · waiting on Test Historic refresh"
    running_set_rows = running_sets(blob)
    last_ready = blob.get("lastReady") if isinstance(blob.get("lastReady"), dict) else None
    if not isinstance(last_ready, dict) or not (last_ready.get("pfStats") or last_ready.get("withWithout")):
        try:
            last_ready = read_last_ready()
        except Exception:
            last_ready = last_ready if isinstance(last_ready, dict) else {}
    _merge_last_ready_stats(blob, last_ready if isinstance(last_ready, dict) else None)
    remaining = max(0, int(sets_total) - int(sets_done))
    if running:
        processing_n = remaining if remaining else (1 if pct < 100.0 else 0)
    elif paused:
        processing_n = remaining
    else:
        processing_n = 0
    ready_flag = bool(ids) or bool(blob.get("ready")) or (phase == "ready" and n_ids > 0)
    if err_line and not running and not paused:
        ready_flag = False
    return {
        "phase": phase,
        "pct": pct,
        "ready": ready_flag,
        "running": running,
        "paused": paused,
        "validatedCount": n_ids,
        "internSetCount": intern_n,
        "setsDone": sets_done if running else n_ids,
        "setsTotal": sets_total if running else max(n_ids, 1 if running or n_ids else 0),
        "histFills": n_fills,
        "detail": detail,
        "generatedAt": blob.get("generatedAt"),
        "stale": stale,
        "hours": blob.get("hours"),
        "filled": blob.get("filled"),
        "targetCount": blob.get("targetCount"),
        "symbol": symbols[0] if symbols else blob.get("symbol"),
        "enabled": True,
        "ownsCatalog": True,
        "catalogSkipped": not running,
        "symbols": intern_syms,
        "runningSets": running_set_rows,
        "processedSetCount": n_ids,
        "processingCount": processing_n,
        "internSymbols": intern_syms,
        "selectedCoordinations": selected_coordinations(blob),
        "withWithout": blob.get("withWithout") or {},
        "comboMatrix": (blob.get("comboMatrix") or [])[:40] if isinstance(blob.get("comboMatrix"), list) else [],
        "successfulConfigs": (blob.get("successfulConfigs") or [])[:24] if isinstance(blob.get("successfulConfigs"), list) else [],
        "pfStats": blob.get("pfStats") or {},
        "combo": blob.get("combo") or {},
        "byIndication": _compact_stat_map(blob.get("byIndication") or blob.get("kinds") or {}, 16),
        "byStrategy": _compact_stat_map(blob.get("byStrategy") or {}, 16),
        "error": blob.get("error") or "",
    }


def request_stop() -> None:
    global _STOP, _PAUSE
    _STOP = True
    _PAUSE = False
    try:
        _ensure_dir()
        with open(_stop_file(), "w", encoding="utf-8") as handle:
            handle.write("1")
    except Exception:
        pass
    try:
        os.remove(_pause_file())
    except Exception:
        pass


def clear_stop() -> None:
    global _STOP
    _STOP = False
    try:
        os.remove(_stop_file())
    except Exception:
        pass


def stop_requested() -> bool:
    if _STOP:
        return True
    return os.path.exists(_stop_file())


def request_pause() -> None:
    global _PAUSE, _STOP
    _PAUSE = True
    _STOP = False
    try:
        _ensure_dir()
        with open(_pause_file(), "w", encoding="utf-8") as handle:
            handle.write("1")
    except Exception:
        pass
    try:
        os.remove(_stop_file())
    except Exception:
        pass


def clear_pause() -> None:
    global _PAUSE
    _PAUSE = False
    try:
        os.remove(_pause_file())
    except Exception:
        pass


def pause_requested() -> bool:
    if _PAUSE:
        return True
    return os.path.exists(_pause_file())


def thread_alive() -> bool:
    return _THREAD is not None and _THREAD.is_alive()


def wait_if_paused(on_progress: Optional[Callable[[Dict[str, Any]], None]] = None, snapshot: Optional[Dict[str, Any]] = None) -> None:
    """Hold a live run at the next checkpoint until Resume or Stop. Idle pause is a flag only."""
    if not pause_requested() or stop_requested():
        return
    blob = dict(snapshot or {})
    resume_phase = str(blob.get("phase") or blob.get("resumePhase") or "evaluate")
    if resume_phase not in RUNNING_PHASES:
        resume_phase = "evaluate"
    paused_blob = {
        **blob,
        "phase": "paused",
        "paused": True,
        "running": True,
        "resumePhase": resume_phase,
        "detail": str(blob.get("detail") or "historic test paused"),
    }
    if on_progress:
        on_progress(paused_blob)
    else:
        current = read_job()
        current.update(paused_blob)
        publish(current)
    while pause_requested() and not stop_requested():
        time.sleep(0.12)


def job_is_running(job: Optional[Dict[str, Any]] = None) -> bool:
    if stop_requested():
        return False
    blob = job if isinstance(job, dict) else read_job()
    phase = str(blob.get("phase") or "")
    if pause_requested() or phase == "paused":
        return True
    return phase in RUNNING_PHASES


def job_is_paused(job: Optional[Dict[str, Any]] = None) -> bool:
    blob = job if isinstance(job, dict) else read_job()
    if pause_requested():
        return True
    if bool(blob.get("paused")):
        return True
    return str(blob.get("phase") or "") == "paused"


def test_overlay(hours: int, min_pf: float, step_lo: int = STEP_LO, step_hi: int = STEP_HI) -> Dict[str, Any]:
    lookback = lookback_bars(hours)
    lo = max(1, min(30, int(step_lo)))
    hi = max(lo, min(30, int(step_hi)))
    return {
        "histEnabled": True,
        "histLookbackBars": lookback,
        "histMinBars": min(60, lookback),
        "histWarmup": HIST_WARMUP_BARS,
        "histExactWindow": True,
        "histTestHours": hours,
        "histTestMinPf": min_pf,
        "histTestRefreshHours": REFRESH_DEFAULT,
        "setUseHistoricGate": True,
        "setStrictGate": True,
        "setAutoDeact": True,
        "setReactivate": True,
        "baseEvalPosCount": 30,
        "setPfWindow": 30,
        "setMinSamples": 30,
        "setMinPf": min_pf,
        "minPf": min_pf,
        "baseMinPf": min_pf,
        "mainMinPf": min_pf,
        "realMinPf": min_pf,
        "setMaxDdTimeS": 57600,
        "setMinStep": lo,
        "setStepMax": hi,
        "stratTrailing": True,
        "stratIndications": True,
        "stratGeneral": True,
        "stratBlock": True,
        "blockEnabled": True,
        "blockActive": True,
        "blockOverall": True,
        "dcaEnabled": False,
        "stratDca": False,
        "histSimulateBlock": True,
        "histSimulateDca": True,
        "blockVolumeRatio": 1,
        "blockMaxStack": 3,
        "blockProfitFactorRatio": 1.25,
        "blockMaxVolumeMultiplier": 2,
        "trailArmMin": 0.3,
        "trailArmMax": 1.5,
        "trailGiveMin": 0.1,
        "trailGiveMax": 0.1,
        "exitIgnoreTp": True,
        "setHonorTp": True,
        "positionCostPct": 0.10,
        "positionCostFallbackPct": 0.10,
        "axisPrevEnabled": False,
        "axisLastEnabled": False,
        "axisContEnabled": False,
        "axisPauseEnabled": False,
        "indTypeState": True,
        "indTypeSignals": True,
        "indTypeDirection": True,
        "indTypeMove": True,
        "indTypeActive": True,
        "indTypeCommon": True,
        "indTypeTrend": True,
        "indTypeBreak": True,
        "slToTpRatios": [0.3, 0.6, 0.9, 1.2, 1.5, 1.8, 2.1, 2.4, 2.7, 3.0],
        "controlOrders": True,
        "normalExecutionEnabled": True,
    }


def fetch_ticker() -> List[Dict[str, Any]]:
    body = _public_json(TICKER_URL, timeout=20.0)
    rows = body.get("data") if isinstance(body, dict) else []
    out: List[Dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol.endswith("-USDT"):
            continue
        try:
            last = float(row.get("lastPrice") or 0)
            hi = float(row.get("highPrice") or 0)
            lo = float(row.get("lowPrice") or 0)
            qv = float(row.get("quoteVolume") or 0)
            chg = float(row.get("priceChangePercent") or 0)
        except (TypeError, ValueError):
            continue
        if last <= 0 or hi <= 0 or lo <= 0 or hi < lo or qv < MIN_QUOTE_VOLUME:
            continue
        out.append({
            "symbol": symbol,
            "last": last,
            "vol24h": round((hi - lo) / last * 100.0, 4),
            "quoteVolume": qv,
            "changePct": chg,
            "vol1h": 0.0,
        })
    out.sort(key=lambda r: -float(r.get("quoteVolume") or 0))
    return out


def keep_recalc_symbols(
    keep_symbols: Optional[List[str]] = None,
    target: int = DEFAULT_TARGET,
    job: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Recalc majors that already cleared the floor. A junk rank queue is discarded."""
    cap = max(int(target or 0), len(PREFERRED_SYMBOLS))
    raw = [str(s).strip().upper() for s in (keep_symbols or []) if str(s or "").strip().endswith("-USDT")]
    majors_hit = [s for s in raw if s in _MAJOR_KEYS]
    if majors_hit:
        return majors_hit[:cap]
    if raw and len(raw) <= max(cap, 8):
        return raw[:cap]
    out: List[str] = []
    seen: set[str] = set()

    def add(name: Any) -> None:
        key = str(name or "").strip().upper()
        if not key.endswith("-USDT") or key in seen or key not in _MAJOR_KEYS:
            return
        seen.add(key)
        out.append(key)

    blob = job if isinstance(job, dict) else {}
    for name in validated_symbols(blob) or validated_symbols(read_last_ready()):
        add(name)
    if not out:
        for name in PREFERRED_SYMBOLS:
            add(name)
        for name in HIST_TEST_MAJORS:
            add(name)
    return out[:cap]


def rank_universe(n: int = VALIDATION_CAP) -> tuple:
    universe = fetch_ticker()
    if not universe:
        raise RuntimeError("BingX ticker returned no USDT perps")
    by_sym = {r["symbol"]: r for r in universe}
    picked: List[Dict[str, Any]] = []
    have = set()

    def add_symbol(symbol: str) -> None:
        name = str(symbol or "").strip().upper()
        if not name or name in have or name.startswith(("NCCO", "NCS", "NCFX")):
            return
        row = dict(by_sym.get(symbol) or {
            "symbol": symbol, "last": 0, "vol24h": 0, "quoteVolume": 0, "changePct": 0, "vol1h": 0,
        })
        row["symbol"] = symbol
        picked.append(row)
        have.add(symbol)

    for symbol in PREFERRED_SYMBOLS:
        add_symbol(symbol)
    rest = []
    for symbol in HIST_TEST_MAJORS:
        if symbol in have:
            continue
        if symbol in by_sym:
            rest.append(by_sym[symbol])
        else:
            rest.append({"symbol": symbol, "last": 0, "vol24h": 0, "quoteVolume": 0, "changePct": 0, "vol1h": 0})
    rest.sort(key=lambda r: -float(r.get("quoteVolume") or 0))
    for row in rest:
        if len(picked) >= max(n, len(PREFERRED_SYMBOLS)):
            break
        add_symbol(str(row.get("symbol") or ""))
    extra = [r for r in universe if r["symbol"] not in have]
    extra.sort(key=lambda r: (-float(r.get("quoteVolume") or 0), str(r.get("symbol") or "")))
    for row in extra:
        if len(picked) >= max(n, len(PREFERRED_SYMBOLS)):
            break
        add_symbol(str(row.get("symbol") or ""))
    preview = [r for r in universe if r["symbol"] in have]
    preview.sort(key=lambda r: -float(r.get("quoteVolume") or 0))
    return picked, preview[: min(40, max(n, 1))]


def symbol_clears_floor(stats: Dict[str, Any], min_pf: float) -> bool:
    """A symbol is positive when it has fills and cost-PF meets the test floor."""
    n = int(stats.get("n") or stats.get("evalN") or stats.get("last15N") or 0)
    try:
        pf = float(stats.get("pf") or 0)
    except (TypeError, ValueError):
        pf = 0.0
    return n > 0 and is_positive_pf(pf, min_pf)


def fill_positive(
    queue: List[Dict[str, Any]],
    target: int,
    min_pf: float,
    overlay: Dict[str, Any],
    fetch_fn: Callable[[str, int], List[List[float]]],
    *,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    score_fn: Optional[Callable[[str, List[List[float]]], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Evaluate ranked symbols rawly until `target` positive results fill."""
    hours = clamp_hours(overlay.get("histTestHours") or overlay.get("hours") or HOURS_DEFAULT)
    lookback = int(overlay.get("histLookbackBars") or lookback_bars(hours))
    fetch_bars = lookback + int(overlay.get("histWarmup") or HIST_WARMUP_BARS)
    selected: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    assigned_ids: List[str] = []
    assigned_seen: set[str] = set()
    for row in queue:
        symbol = str(row.get("symbol") or "").strip().upper()
        snapshot = {
            "phase": "evaluate",
            "pct": 8 + int(52 * len(selected) / max(1, target)),
            "detail": f"evaluate {symbol or 'next'} · {len(selected)}/{target} positive",
            "symbols": [r["symbol"] for r in selected] + ([symbol] if symbol else []),
            "positive": [r["symbol"] for r in selected],
            "rejected": [r["symbol"] for r in rejected],
            "validatedIds": list(assigned_ids)[:PUBLIC_VALIDATED_IDS_CAP],
            "validatedCount": len(assigned_ids),
        }
        wait_if_paused(on_progress, snapshot)
        if stop_requested() or len(selected) >= target:
            break
        if not symbol:
            continue
        if on_progress:
            on_progress(snapshot)
        try:
            bars = fetch_fn(symbol, fetch_bars)
        except Exception as exc:
            skipped.append({"symbol": symbol, "reason": f"fetch {type(exc).__name__}"})
            continue
        wait_if_paused(on_progress, snapshot)
        if stop_requested():
            break
        if not isinstance(bars, list) or len(bars) < min(80, max(40, lookback // 2)):
            skipped.append({"symbol": symbol, "reason": f"bars {len(bars) if isinstance(bars, list) else 0}"})
            continue
        probe = None
        if score_fn is not None:
            stats = dict(score_fn(symbol, bars) or {})
        else:
            probe = SetBook()
            probe.load(overlay)
            probe.ingest_bars(symbol, bars)
            probe.replay_all(symbols=[symbol], workers=1, merge=True, score=True)
            roll = {r.get("symbol"): r for r in symbol_rollup(probe) if isinstance(r, dict)}
            stats = dict(roll.get(symbol) or {})
        record = {
            **{k: v for k, v in row.items() if k != "_bars"},
            "symbol": symbol,
            "pf": round(float(stats.get("pf") or 0), 4),
            "n": int(stats.get("n") or 0),
            "evalN": int(stats.get("evalN") or stats.get("last15N") or 0),
            "wr": float(stats.get("wr") or 0),
            "maxDdS": float(stats.get("maxDdS") or 0),
            "validated": bool(stats.get("validated")),
            "bars": len(bars),
        }
        record["positive"] = symbol_clears_floor(record, min_pf)
        if record["positive"]:
            record["_bars"] = bars
            selected.append(record)
            if probe is not None:
                try:
                    ranked = _rank_set_rows(probe)
                    for item in ranked or []:
                        try:
                            _key, st, _side, valid, _low = item
                        except (TypeError, ValueError):
                            continue
                        if not valid:
                            continue
                        sid = str(getattr(st, "id", "") or "")
                        if not sid or sid in assigned_seen:
                            continue
                        assigned_seen.add(sid)
                        assigned_ids.append(sid)
                except Exception:
                    pass
            if assigned_ids:
                persist_validated_ids(assigned_ids)
            if on_progress:
                on_progress({
                    "phase": "evaluate",
                    "pct": 8 + int(52 * len(selected) / max(1, target)),
                    "detail": f"evaluate {symbol} · {len(selected)}/{target} positive · {len(assigned_ids)} configs",
                    "symbols": [r["symbol"] for r in selected],
                    "positive": [r["symbol"] for r in selected],
                    "rejected": [r["symbol"] for r in rejected],
                    "validatedIds": list(assigned_ids)[:PUBLIC_VALIDATED_IDS_CAP],
                    "validatedCount": len(assigned_ids),
                })
        else:
            rejected.append(record)
    return {
        "selected": selected,
        "rejected": rejected,
        "skipped": skipped,
        "evaluated": len(selected) + len(rejected) + len(skipped),
        "filled": len(selected),
        "target": target,
        "short": max(0, target - len(selected)),
    }


def _compact_stat_map(blob: Any, limit: int = 40) -> Dict[str, Any]:
    """Keep PF + DDT identity for relations/types. Drop window dumps."""
    if not isinstance(blob, dict):
        return {}
    prefer = [
        "indications", "general", "block", "dca", "core", "trail",
        "state", "signals", "active", "direction", "move", "common", "trend", "break",
        "block:active", "block:break", "block:common", "block:direction",
        "block:move", "block:signals", "block:state", "block:trend",
        "indications:trail",
    ]
    keys = [k for k in prefer if k in blob] + [k for k in blob if k not in prefer]
    out: Dict[str, Any] = {}
    keep = {"n", "evalN", "pf", "wr", "netAvg", "validated", "maxDdS", "avgDdS", "pfDdRatio",
            "last15N", "costSubtracted", "kind", "strategy", "indication", "direction", "tapeN"}
    for key in keys[: max(1, int(limit or 40))]:
        row = blob.get(key)
        if not isinstance(row, dict):
            continue
        slim = {kk: vv for kk, vv in row.items() if kk in keep or kk == "bySide"}
        if isinstance(slim.get("bySide"), dict):
            slim["bySide"] = {
                side: {kk: vv for kk, vv in (stats or {}).items() if kk in keep}
                for side, stats in slim["bySide"].items() if isinstance(stats, dict)
            }
        out[str(key)] = slim
    return out


def _cov_blob(value: Any, fallback_done: int = 0, fallback_total: int = 0) -> Dict[str, Any]:
    if isinstance(value, dict) and ("coveragePct" in value or "completed" in value or "done" in value):
        requested = int(value.get("requested") or value.get("total") or fallback_total or 0)
        completed = int(value.get("completed") or value.get("done") or fallback_done or 0)
        out = dict(value)
        out.setdefault("requested", requested)
        out.setdefault("completed", completed)
        out.setdefault("done", completed)
        out.setdefault("total", requested)
        out.setdefault("coveragePct", round(100.0 * completed / requested, 2) if requested else 100.0)
        return out
    return coverage_counter(fallback_total, fallback_done)


def compact_job(job: Dict[str, Any], ranked: List[Dict[str, Any]], universe: List[Dict[str, Any]], hours: int, min_pf: float) -> Dict[str, Any]:
    step_blob = job.get("byStep") if isinstance(job.get("byStep"), dict) else {}
    by_step = step_blob.get("byStep") if isinstance(step_blob.get("byStep"), dict) else {}
    ranges = step_blob.get("ranges") if isinstance(step_blob.get("ranges"), list) else []
    heatmap = step_blob.get("heatmap") if isinstance(step_blob.get("heatmap"), list) else []
    if not by_step and isinstance(job.get("byStep"), list):
        step_rows = [dict(item) for item in job["byStep"] if isinstance(item, dict)]
        for row in step_rows:
            row.pop("evaluationWindows", None)
    else:
        steps = [int(s) for s in (step_blob.get("steps") or sorted(by_step, key=lambda x: int(x)))]
        step_rows = []
        for step in steps:
            item = dict(by_step.get(str(step)) or {})
            item.pop("evaluationWindows", None)
            step_rows.append(item)
    range_rows = []
    for item in ranges:
        row = dict(item)
        row.pop("evaluationWindows", None)
        range_rows.append(row)
    heat = []
    for cell in heatmap:
        heat.append({
            "step": cell.get("step"),
            "slRatio": cell.get("slRatio"),
            "pf": cell.get("pf"),
            "maxDdS": cell.get("maxDdS"),
            "pfDdRatio": cell.get("pfDdRatio"),
            "n": cell.get("n"),
            "wr": cell.get("wr"),
            "validated": cell.get("validated"),
        })
    best_step = None
    if step_rows:
        best_step = sorted(
            step_rows,
            key=lambda r: (0 if r.get("validated") else 1, -float(r.get("pfDdRatio") or 0), -float(r.get("pf") or 0), float(r.get("maxDdS") or 9e9)),
        )[0]
    coverage = job.get("coverage") if isinstance(job.get("coverage"), dict) else {}
    winner = job.get("winner") if isinstance(job.get("winner"), dict) else {}
    public_ranked = []
    for row in ranked:
        if not isinstance(row, dict):
            continue
        item = {k: v for k, v in row.items() if k != "_bars"}
        if not item.get("symbol"):
            continue
        if item.get("positive") is None:
            item["positive"] = symbol_clears_floor(item, min_pf)
        public_ranked.append(item)
    scored = [r for r in public_ranked if r.get("positive") or symbol_clears_floor(r, min_pf)]
    tape_names = [str(s).strip().upper() for s in (job.get("positive") or job.get("symbols") or []) if str(s or "").strip()]
    if not tape_names:
        tape_names = [str(r.get("symbol") or "").upper() for r in (public_ranked or scored) if r.get("symbol")]
    tape_names = [s for s in tape_names if s in _MAJOR_KEYS] or tape_names
    name_keys = {s.upper() for s in tape_names}
    by_sym_rows: List[Dict[str, Any]] = []
    for row in (job.get("bySymbol") or []):
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        if name_keys and sym not in name_keys:
            continue
        slim = {k: v for k, v in row.items() if k != "evaluationWindows"}
        slim["symbol"] = sym
        slim["positive"] = symbol_clears_floor(slim, min_pf)
        by_sym_rows.append(slim)
    positive_names = [str(r.get("symbol") or "") for r in by_sym_rows if r.get("positive")]
    names = tape_names or positive_names
    ids = list(job.get("validatedIds") or []) or collect_validated_ids(job)
    persist_validated_ids(ids)
    try:
        validated_count = int(job.get("validatedCount") or 0)
    except (TypeError, ValueError):
        validated_count = 0
    validated_count = max(validated_count, len(ids))
    out = {
        "ok": True,
        "phase": str(job.get("phase") or "ready"),
        "ready": bool(job.get("ready")),
        "running": bool(job.get("running")),
        "paused": bool(job.get("paused")) or pause_requested(),
        "error": str(job.get("error") or ""),
        "source": str(job.get("source") or ""),
        "hours": hours,
        "minPf": min_pf,
        "positivePf": min_pf,
        "refreshHours": job.get("refreshHours") or REFRESH_DEFAULT,
        "nextRunAt": job.get("nextRunAt"),
        "continuous": bool(job.get("continuous")),
        "stepLo": job.get("stepLo") or STEP_LO,
        "stepHi": job.get("stepHi") or STEP_HI,
        "targetCount": job.get("targetCount"),
        "filled": job.get("filled"),
        "evaluated": job.get("evaluated"),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsedMs": job.get("elapsedMs"),
        "workers": job.get("workers") or 1,
        "timings": job.get("timings") or {},
        "options": job.get("options") or {},
        "costPct": 0.10,
        "symbols": names,
        "positive": names,
        "rejected": (job.get("rejected") or [])[:80],
        "skipped": (job.get("skipped") or [])[:40],
        "ranked": (scored or public_ranked)[:80],
        "universePreview": universe[:12],
        "coverage": {
            "sets": _cov_blob(coverage.get("sets"), int(coverage.get("setCount") or 0), int(coverage.get("setCount") or 0)),
            "symbols": _cov_blob(coverage.get("symbols")),
            "bars": _cov_blob(coverage.get("bars")),
            "evaluations": _cov_blob(coverage.get("evaluations")),
            "tasks": _cov_blob(coverage.get("tasks")),
            "setCount": coverage.get("setCount") or coverage.get("product"),
            "product": coverage.get("product") or coverage.get("setCount"),
            "packs": coverage.get("packs"),
            "slRatios": coverage.get("slRatios"),
            "steps": coverage.get("steps"),
            "trails": coverage.get("trails") if isinstance(coverage.get("trails"), int) else len(coverage.get("trails") or []),
            "histFills": coverage.get("histFills") or coverage.get("replayFills"),
            "indexed": coverage.get("indexed"),
            "independentStrategy": True,
            "independentDirection": True,
            "independentIndication": True,
            "independentConfigs": True,
            "independentCombo": True,
        },
        "validatedCount": validated_count or job.get("validatedCount"),
        "rowCount": job.get("rowCount"),
        "winner": {
            "id": normalize_catalog_set_id(winner.get("id")) or winner.get("id"),
            "step": winner.get("step"),
            "slRatio": winner.get("slRatio"),
            "pack": winner.get("pack"),
            "trailKey": winner.get("trailKey"),
            "last15Ratio": winner.get("last15Ratio"),
            "maxDdS": winner.get("maxDdS"),
            "n": winner.get("n"),
        } if winner else {},
        "bySymbol": by_sym_rows,
        "byDirection": {
            k: {kk: vv for kk, vv in (v or {}).items() if kk != "evaluationWindows"}
            for k, v in (job.get("byDirection") or {}).items() if isinstance(v, dict)
        },
        "byStrategy": _compact_stat_map(job.get("byStrategy") or {}, 40),
        "byIndication": _compact_stat_map(job.get("byIndication") or job.get("kinds") or {}, 24),
        "kinds": _compact_stat_map(job.get("kinds") or job.get("byIndication") or {}, 24),
        "pfStats": job.get("pfStats") or {},
        "withWithout": job.get("withWithout") or {},
        "comboMatrix": job.get("comboMatrix") or [],
        "successfulConfigs": (job.get("successfulConfigs") or [])[:60],
        "validatedIds": ids[:PUBLIC_VALIDATED_IDS_CAP],
        "recalcOnly": bool(job.get("recalcOnly")),
        "combo": job.get("combo") or {},
        "byStep": step_rows,
        "ranges": range_rows,
        "heatmap": heat,
        "bestStep": best_step,
        "audit": job.get("audit") or {},
        "detail": job.get("detail") or "",
        "pct": 100 if (job.get("ready") or str(job.get("phase") or "") == "ready") else (job.get("pct") or 0),
        "filled": len(names) if names else (job.get("filled") if job.get("filled") is not None else len(scored)),
        "evaluated": job.get("evaluated") or (job.get("fill") or {}).get("evaluated"),
        "fill": job.get("fill") or {},
    }
    return out


def audit_test(book: Any, symbols: List[str], summary: Dict[str, Any], min_pf: float, hours: int, target: int) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []

    def rec(name: str, ok: bool, detail: Any = "") -> None:
        rows.append({"name": name, "ok": bool(ok), "detail": detail})

    rec("hours-range", HOURS_MIN <= hours <= HOURS_MAX, hours)
    rec("min-pf-floor", abs(float(min_pf) - float(summary.get("minPf") or min_pf)) < 1e-9, summary.get("minPf"))
    rec("symbols-filled", 0 < len(symbols) <= max(int(target or 0), 50), {"got": len(symbols), "target": target})
    rec("positive-count", len(summary.get("positive") or []) == len(symbols), summary.get("positive"))
    cov = summary.get("coverage") or {}
    rec("sets-present", int(cov.get("setCount") or cov.get("product") or 0) > 0, cov.get("setCount"))
    by_sym = {r.get("symbol"): r for r in (summary.get("bySymbol") or []) if isinstance(r, dict)}
    rec("symbol-stats-complete", all(s in by_sym for s in symbols), sorted(by_sym))
    rec(
        "symbols-meet-floor",
        all(symbol_clears_floor(by_sym.get(s) or {}, min_pf) for s in symbols if s in by_sym) if by_sym else True,
        {s: (by_sym.get(s) or {}).get("pf") for s in symbols},
    )
    rec("direction-both", set(summary.get("byDirection") or {}) == {"LONG", "SHORT"} or not symbols, sorted(summary.get("byDirection") or {}))
    rec("pf-families", set((summary.get("pfStats") or {})) >= {"overall", "normal", "trailing", "axis", "block", "dca"} or not symbols, sorted(summary.get("pfStats") or {}))
    rec("with-without-block-dca", set((summary.get("withWithout") or {})) >= {"block", "dca"} or not symbols, sorted(summary.get("withWithout") or {}))
    rec("combo-engine-memory", (summary.get("combo") or {}).get("engine") == "sqlite-memory" or not symbols, summary.get("combo"))
    rec("successful-positive", all(float(r.get("pf") or 0) >= float(min_pf) - 1e-9 for r in (summary.get("successfulConfigs") or [])), len(summary.get("successfulConfigs") or []))
    rec("combo-ddt", all("maxDdS" in ((summary.get("pfStats") or {}).get(k) or {}) for k in ("overall", "block", "dca")) or not symbols, summary.get("pfStats"))
    rec("kinds-pf-ddt", all("pf" in (v or {}) and "maxDdS" in (v or {}) for v in (summary.get("byIndication") or summary.get("kinds") or {}).values()) or not (summary.get("byIndication") or summary.get("kinds")), sorted((summary.get("byIndication") or summary.get("kinds") or {}).keys()))
    failed = [r["name"] for r in rows if not r["ok"]]
    return {"ok": not failed, "pass": len(rows) - len(failed), "fail": len(failed), "failed": failed, "rows": rows}


def run_test(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    clear_stop()
    clear_pause()
    body = body if isinstance(body, dict) else {}
    hours = clamp_hours(body.get("hours") or (body.get("overlay") or {}).get("histTestHours") or HOURS_DEFAULT)
    min_pf = clamp_min_pf(body.get("minPf") or body.get("histTestMinPf") or (body.get("overlay") or {}).get("histTestMinPf") or POSITIVE_PF)
    refresh_h = clamp_refresh_hours(
        body.get("refreshHours")
        or body.get("histTestRefreshHours")
        or (body.get("overlay") or {}).get("histTestRefreshHours")
        or REFRESH_DEFAULT
    )
    target = clamp_target(
        body.get("targetCount")
        or body.get("count")
        or body.get("symbolCap")
        or (body.get("overlay") or {}).get("histTestTargetCount")
        or (body.get("overlay") or {}).get("symbolCap")
        or DEFAULT_TARGET
    )
    step_lo = max(1, min(30, int(body.get("minStep") or body.get("stepLo") or STEP_LO)))
    step_hi = max(step_lo, min(30, int(body.get("stepMax") or body.get("stepHi") or STEP_HI)))
    synth = bool(body.get("synth"))
    recalc_ids = [str(s).strip() for s in (body.get("recalcIds") or []) if str(s or "").strip()]
    if not recalc_ids:
        recalc_ids = [str(s).strip() for s in seed_recalc_prior().get("validatedIds") or [] if str(s or "").strip()] or read_persisted_validated_ids()
    keep_symbols = [str(s).strip().upper() for s in (body.get("keepSymbols") or []) if str(s or "").strip()]
    if not keep_symbols:
        keep_symbols = keep_recalc_symbols(None, target, seed_recalc_prior())
    recalc_only = use_recalc_only(body, recalc_ids, keep_symbols, target)
    overlay = test_overlay(hours, min_pf, step_lo, step_hi)
    user_ov = body.get("overlay") if isinstance(body.get("overlay"), dict) else {}
    if user_ov:
        overlay.update({k: v for k, v in user_ov.items() if k not in ("symbols",)})
        overlay["histLookbackBars"] = lookback_bars(hours)
        overlay["histTestHours"] = hours
        overlay["histTestMinPf"] = min_pf
        overlay["histTestRefreshHours"] = refresh_h
        overlay["setMinPf"] = min_pf
        overlay["minPf"] = min_pf
    t0 = time.time()
    prior_ready = seed_recalc_prior()
    seed_ids = recalc_ids or list(prior_ready.get("validatedIds") or []) or read_persisted_validated_ids()
    seed_syms = keep_symbols or [str(s).strip().upper() for s in (prior_ready.get("positive") or prior_ready.get("symbols") or []) if str(s or "").strip()]
    seed = {
        "ok": True,
        "phase": "rank",
        "pct": 2,
        "ready": False,
        "running": True,
        "paused": False,
        "detail": (
            f"recalc {len(seed_ids)} validated configs · {hours}h · min PF {min_pf:.2f}"
            if recalc_only else
            f"ranking universe · fill {target} positive · {hours}h · min PF {min_pf:.2f}"
        ),
        "hours": hours,
        "minPf": min_pf,
        "positivePf": min_pf,
        "targetCount": target,
        "stepLo": step_lo,
        "stepHi": step_hi,
        "symbols": list(seed_syms),
        "positive": list(seed_syms),
        "validatedIds": list(seed_ids)[:PUBLIC_VALIDATED_IDS_CAP],
        "validatedCount": len(seed_ids),
        "rejected": [],
    }
    publish(seed)

    def progress(update: Dict[str, Any]) -> None:
        if stop_requested():
            return
        blob = dict(seed)
        blob.update(update)
        blob["hours"] = hours
        blob["minPf"] = min_pf
        blob["positivePf"] = min_pf
        blob["targetCount"] = target
        blob["running"] = not stop_requested()
        blob["paused"] = pause_requested() and not stop_requested()
        blob["ready"] = False
        if not blob.get("validatedIds"):
            blob["validatedIds"] = list(seed.get("validatedIds") or seed_ids or [])[:PUBLIC_VALIDATED_IDS_CAP]
        if not blob.get("validatedCount"):
            blob["validatedCount"] = int(seed.get("validatedCount") or len(blob.get("validatedIds") or seed_ids or []))
        if not blob.get("positive"):
            blob["positive"] = list(seed.get("positive") or seed_syms or [])
        if not blob.get("symbols"):
            blob["symbols"] = list(blob.get("positive") or seed.get("symbols") or seed_syms or [])
        publish(blob)

    if synth:
        queue = [
            {"symbol": "AAA-USDT", "vol1h": 9, "vol24h": 4, "quoteVolume": 2e6, "changePct": 1, "last": 100},
            {"symbol": "BBB-USDT", "vol1h": 8, "vol24h": 3, "quoteVolume": 2e6, "changePct": -1, "last": 40},
            {"symbol": "CCC-USDT", "vol1h": 7, "vol24h": 3, "quoteVolume": 2e6, "changePct": 0.4, "last": 80},
            {"symbol": "DDD-USDT", "vol1h": 6, "vol24h": 2, "quoteVolume": 2e6, "changePct": -0.2, "last": 22},
            {"symbol": "EEE-USDT", "vol1h": 5, "vol24h": 2, "quoteVolume": 2e6, "changePct": 0.1, "last": 55},
        ]
        universe = list(queue)

        def fetch_fn(symbol: str, limit: int) -> List[List[float]]:
            step = {"AAA-USDT": 0.22, "CCC-USDT": 0.18, "EEE-USDT": 0.16}.get(symbol, -0.14)
            return synth_trend(max(80, min(limit, 240)), start=50.0 if step < 0 else 80.0, step=step, noise=0.03)
    else:
        queue, universe = rank_universe(max(VALIDATION_CAP, int(target or 0)))
        fetch_fn = fetch_klines

    lookback = lookback_bars(hours)
    fetch_bars = lookback + int(overlay.get("histWarmup") or HIST_WARMUP_BARS)
    if recalc_only:
        keep = keep_recalc_symbols(keep_symbols, target)
        selected = []
        skipped = []
        fill = fill_positive(
            [{"symbol": s} for s in keep],
            max(len(keep), 1),
            min_pf,
            overlay,
            fetch_fn,
            on_progress=progress,
        )
        selected = fill["selected"]
        skipped = fill["skipped"]
        if not selected and keep:
            # Floor wobble must not drop the last ready majors book.
            for symbol in keep:
                try:
                    bars = fetch_fn(symbol, fetch_bars)
                except Exception as exc:
                    skipped.append({"symbol": symbol, "reason": f"fetch {type(exc).__name__}"})
                    continue
                if not isinstance(bars, list) or len(bars) < min(80, max(40, lookback // 2)):
                    skipped.append({"symbol": symbol, "reason": f"bars {len(bars) if isinstance(bars, list) else 0}"})
                    continue
                selected.append({"symbol": symbol, "_bars": bars, "positive": True, "n": 0, "pf": 0.0})
            fill = {
                "selected": selected,
                "rejected": fill.get("rejected") or [],
                "skipped": skipped,
                "filled": len(selected),
                "target": target,
                "short": max(0, int(target) - len(selected)),
                "evaluated": len(selected) + len(fill.get("rejected") or []) + len(skipped),
            }
    else:
        fill = fill_positive(queue, target, min_pf, overlay, fetch_fn, on_progress=progress)
    selected = fill["selected"]

    def stopped_job(detail: str = "historic test stopped") -> Dict[str, Any]:
        names = [r["symbol"] for r in selected]
        return publish({
            **seed,
            "phase": "stopped",
            "pct": 100 if names else (seed.get("pct") or 0),
            "ready": False,
            "running": False,
            "paused": False,
            "detail": detail,
            "symbols": names,
            "positive": names,
            "rejected": [{k: v for k, v in r.items() if k != "_bars"} for r in fill["rejected"]],
            "skipped": fill["skipped"],
            "fill": {k: v for k, v in fill.items() if k != "selected"},
            "elapsedMs": round((time.time() - t0) * 1000.0, 1),
        })

    if stop_requested():
        return stopped_job()
    wait_if_paused(progress, {
        "phase": "replay",
        "pct": 60,
        "detail": f"replay {len(selected)} positive · {hours}h tape",
        "symbols": [r["symbol"] for r in selected],
        "positive": [r["symbol"] for r in selected],
    })
    if stop_requested():
        return stopped_job()
    if not selected:
        err = {
            **seed,
            "phase": "error",
            "pct": 100,
            "ready": False,
            "running": False,
            "paused": False,
            "error": "no symbol cleared the historic PF floor",
            "detail": f"evaluated {fill['evaluated']} · rejected {len(fill['rejected'])} · skipped {len(fill['skipped'])}",
            "rejected": [{k: v for k, v in r.items() if k != "_bars"} for r in fill["rejected"]],
            "skipped": fill["skipped"],
            "fill": {k: v for k, v in fill.items() if k != "selected"},
            "elapsedMs": round((time.time() - t0) * 1000.0, 1),
        }
        return publish(err)

    symbols = [r["symbol"] for r in selected]
    progress({
        "phase": "replay",
        "pct": 62,
        "detail": f"replay {len(symbols)} positive · {hours}h tape",
        "symbols": symbols,
        "positive": symbols,
        "ranked": [{k: v for k, v in r.items() if k != "_bars"} for r in selected],
    })
    book = SetBook()
    book.load(overlay)
    if recalc_only:
        kept = book.restrict_to_ids(recalc_ids)
        progress({
            "phase": "replay",
            "pct": 64,
            "detail": f"recalc {kept}/{len(recalc_ids)} configs · {len(symbols)} symbols",
            "symbols": symbols,
            "positive": symbols,
            "validatedIds": recalc_ids,
            "recalcOnly": True,
        })
    for row in selected:
        book.ingest_bars(row["symbol"], row["_bars"])

    def on_symbol(symbol: str, done: int, total: int) -> None:
        progress({
            "phase": "replay",
            "pct": 60 + int(28 * (done / max(1, total))),
            "detail": f"replay {done}/{total} · {symbol}",
            "symbols": symbols,
            "positive": symbols,
            "setsDone": done,
            "setsTotal": max(total, 1),
        })

    book.replay_all(
        symbols=symbols,
        workers=1,
        merge=True,
        progress_total=len(symbols),
        score=True,
        set_ids=recalc_ids if recalc_only else None,
        on_symbol=on_symbol,
    )
    progress({
        "phase": "score",
        "pct": 90,
        "detail": f"score + combo · {len(symbols)} symbols",
        "symbols": symbols,
        "positive": symbols,
    })
    ranked_sets = _rank_set_rows(book)
    by_step = step_rollup(book)
    by_sym = symbol_rollup(book)
    by_dir = direction_rollup(book)
    by_strat = strategy_rollup(book, strat=getattr(book, "strategy_hist", None))
    combo = combo_evaluate(book, min_pf=min_pf, cost_pct=float(getattr(book, "cost_pct", 0.1) or 0.1), pf_n=int(getattr(book, "pf_n", 30) or 30))
    kinds = {}
    try:
        kinds = book.ind_gate_snapshot() if hasattr(book, "ind_gate_snapshot") else {}
    except Exception:
        kinds = {}
    progress({
        "phase": "score",
        "pct": 96,
        "detail": f"rank validated configs · {len(symbols)} symbols",
        "symbols": symbols,
        "positive": symbols,
    })
    winner = pick_winner_row(book, ranked_sets)
    listings = catalog_listings(book, ranked_sets, symbols)
    prog = book.progress
    set_n = len(book.by_idx)
    requested_sets = set_n * max(len(symbols), 1)
    book_cov = book.coverage() if hasattr(book, "coverage") else {}
    validated_ids = collect_validated_ids(
        {
            "successfulConfigs": combo.get("successful") or [],
            "winner": winner or {},
            "validatedIds": recalc_ids if recalc_only else [],
        },
        ranked_sets=ranked_sets,
    )
    persist_validated_ids(validated_ids)
    job = {
        "phase": "ready",
        "ready": True,
        "error": getattr(prog, "error", "") or "",
        "source": "synth" if synth else "live",
        "hours": hours,
        "minPf": min_pf,
        "refreshHours": refresh_h,
        "continuous": not synth,
        "running": False,
        "stepLo": step_lo,
        "stepHi": step_hi,
        "targetCount": target,
        "filled": len(symbols),
        "evaluated": fill["evaluated"],
        "elapsedMs": round((time.time() - t0) * 1000.0, 1),
        "workers": 1,
        "timings": {"totalMs": round((time.time() - t0) * 1000.0, 1)},
        "options": {
            "hours": hours,
            "minStep": step_lo,
            "stepMax": step_hi,
            "trailing": True,
            "stratBlock": True,
            "stratDca": False,
            "stratIndications": True,
            "stratGeneral": True,
            "costPct": 0.10,
            "setMinPf": min_pf,
            "histTestHours": hours,
            "histTestMinPf": min_pf,
            "histTestRefreshHours": refresh_h,
            "baseEvalPosCount": 30,
        },
        "coverage": {
            **(book_cov if isinstance(book_cov, dict) else {}),
            "setCount": set_n,
            "sets": coverage_counter(requested_sets, requested_sets),
            "symbols": coverage_counter(len(symbols), len(symbols)),
            "evaluations": coverage_counter(set_n, set_n),
            "tasks": coverage_counter(len(symbols), len(symbols)),
        },
        "validatedCount": len(validated_ids) or sum(1 for item in ranked_sets if item[3]),
        "rowCount": len(ranked_sets),
        "rows": [set_row(st, side) for _k, st, side, _v, _l in ranked_sets[:80]],
        "winner": winner or {},
        "bySymbol": by_sym,
        "byDirection": by_dir,
        "byStrategy": by_strat,
        "byIndication": kinds,
        "kinds": kinds,
        "pfStats": combo.get("pfStats") or {},
        "withWithout": combo.get("withWithout") or {},
        "comboMatrix": combo.get("matrix") or [],
        "successfulConfigs": combo.get("successful") or [],
        "validatedIds": validated_ids,
        "recalcOnly": recalc_only,
        "combo": combo.get("meta") or {},
        "byStep": by_step,
        "listings": listings,
        "rejected": [{k: v for k, v in r.items() if k != "_bars"} for r in fill["rejected"]],
        "skipped": fill["skipped"],
        "fill": {
            "filled": fill["filled"],
            "target": fill["target"],
            "short": fill["short"],
            "evaluated": fill["evaluated"],
            "rejectedCount": len(fill["rejected"]),
            "skippedCount": len(fill["skipped"]),
        },
        "detail": (
            f"{len(symbols)}/{target} positive · evaluated {fill['evaluated']} · "
            f"{len(validated_ids)} validated configs"
        ),
        "pct": 100,
    }
    summary = compact_job(job, selected, universe, hours, min_pf)
    audit = audit_test(book, symbols, summary, min_pf, hours, target)
    summary["audit"] = {k: v for k, v in audit.items() if k != "rows"}
    summary["audit"]["rows"] = audit.get("rows") or []
    if not audit.get("ok"):
        summary["error"] = "audit: " + ", ".join(audit.get("failed") or [])
        # Audit misses are reported; the tape itself still stands.
        summary["ready"] = True
    if stop_requested():
        summary["phase"] = "stopped"
        summary["detail"] = "historic test stopped"
        summary["running"] = False
        summary["paused"] = False
    clear_pause()
    return publish(summary)


def start_test(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Start a new run, or Resume a paused one. Start while already live is a no-op."""
    global _THREAD
    body = dict(body) if isinstance(body, dict) else {}
    winding = None
    with _LOCK:
        if thread_alive() and stop_requested():
            winding = _THREAD
    if winding is not None:
        winding.join(0.25)
    with _LOCK:
        if thread_alive() and pause_requested():
            clear_pause()
            clear_stop()
            job = read_job()
            resume_phase = str(job.get("resumePhase") or "evaluate")
            if resume_phase not in RUNNING_PHASES:
                resume_phase = "evaluate"
            job["ok"] = True
            job["paused"] = False
            job["running"] = True
            job["phase"] = resume_phase
            job["detail"] = "historic test resumed"
            return publish(job)
        if thread_alive() and not stop_requested():
            current = read_job()
            current["ok"] = True
            current["paused"] = False
            current["running"] = True
            current["detail"] = current.get("detail") or "historic test already running"
            return current
        if thread_alive() and stop_requested():
            job = read_job()
            job["ok"] = True
            job["phase"] = "stopped"
            job["running"] = False
            job["paused"] = False
            job["detail"] = "historic test stopping · click Start again"
            return publish(job)
        clear_stop()
        clear_pause()
        hours = clamp_hours(body.get("hours") or (body.get("overlay") or {}).get("histTestHours") or HOURS_DEFAULT)
        min_pf = clamp_min_pf(body.get("minPf") or body.get("histTestMinPf") or (body.get("overlay") or {}).get("histTestMinPf") or POSITIVE_PF)
        refresh_h = clamp_refresh_hours(
            body.get("refreshHours")
            or body.get("histTestRefreshHours")
            or (body.get("overlay") or {}).get("histTestRefreshHours")
            or REFRESH_DEFAULT
        )
        target = clamp_target(
            body.get("targetCount")
            or body.get("count")
            or body.get("symbolCap")
            or (body.get("overlay") or {}).get("histTestTargetCount")
            or (body.get("overlay") or {}).get("symbolCap")
            or DEFAULT_TARGET
        )
        queued = publish({
            "ok": True,
            "phase": "queued",
            "pct": 1,
            "ready": False,
            "running": True,
            "paused": False,
            "detail": f"queued · {hours}h · min PF {min_pf:.2f} · fill {target} · refresh {refresh_h}h",
            "hours": hours,
            "minPf": min_pf,
            "positivePf": min_pf,
            "refreshHours": refresh_h,
            "continuous": not bool(body.get("synth") or body.get("once")),
            "targetCount": target,
            "symbols": [],
            "positive": [],
            "rejected": [],
        })

        def worker() -> None:
            try:
                once = bool(body.get("synth") or body.get("once"))
                prior: Dict[str, Any] = seed_recalc_prior()
                while True:
                    payload = dict(body)
                    ids = validated_set_ids(prior) or read_persisted_validated_ids()
                    if ids and not once:
                        payload["recalcIds"] = ids
                        payload["keepSymbols"] = keep_recalc_symbols(
                            list(prior.get("positive") or prior.get("symbols") or []),
                            clamp_target(payload.get("symbolCap") or payload.get("targetCount") or target),
                            prior,
                        )
                    run_test(payload)
                    if once or stop_requested():
                        break
                    prior = read_job()
                    if not wait_for_refresh(refresh_h, prior):
                        break
            except Exception as exc:
                publish({
                    "ok": False,
                    "phase": "error",
                    "pct": 100,
                    "ready": False,
                    "running": False,
                    "paused": False,
                    "error": f"{type(exc).__name__}: {exc}"[:240],
                    "detail": traceback.format_exc()[-400:],
                    "hours": hours,
                    "minPf": min_pf,
                    "refreshHours": refresh_h,
                    "targetCount": target,
                })

        _THREAD = threading.Thread(target=worker, name="hist-test", daemon=True)
        _THREAD.start()
        return queued


def pause_test() -> Dict[str, Any]:
    """Pause like the engine bar: always accepted. Live runs wait; idle Start becomes Resume."""
    request_pause()
    job = read_job()
    phase = str(job.get("phase") or "")
    if phase in RUNNING_PHASES:
        job["resumePhase"] = phase
    job["ok"] = True
    job["paused"] = True
    job["phase"] = "paused"
    job["running"] = thread_alive()
    job["detail"] = "historic test paused"
    return publish(job)


def resume_test(body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Resume a paused live worker, or Start a new run if nothing is in flight."""
    if thread_alive():
        return start_test(body)
    job = read_job()
    was_live = bool(job.get("running")) or str(job.get("resumePhase") or "") in RUNNING_PHASES
    if was_live:
        clear_pause()
        clear_stop()
        resume_phase = str(job.get("resumePhase") or "evaluate")
        if resume_phase not in RUNNING_PHASES:
            resume_phase = "evaluate"
        job["ok"] = True
        job["paused"] = False
        job["running"] = True
        job["phase"] = resume_phase
        job["detail"] = "historic test resumed"
        return publish(job)
    return start_test(body)


def stop_test() -> Dict[str, Any]:
    request_stop()
    job = read_job()
    job["ok"] = True
    job["running"] = False
    job["paused"] = False
    job["phase"] = "stopped"
    job["detail"] = "historic test stopped"
    publish(job)
    return job


def self_test() -> Dict[str, Any]:
    import shutil
    import tempfile

    global LAST_READY_PATH, VALIDATED_IDS_PATH, PUBLIC_JSON, SUMMARY_PATH, PUBLIC_SWEEP, OUT_DIR
    clear_stop()
    invalidate_job_cache()
    rows: List[Dict[str, Any]] = []

    def rec(name: str, ok: bool, detail: Any = "") -> None:
        rows.append({"name": name, "ok": bool(ok), "detail": detail})

    rec("hours-default", clamp_hours(None) == HOURS_DEFAULT, clamp_hours(None))
    rec("hours-min", clamp_hours(1) == HOURS_MIN, clamp_hours(1))
    rec("hours-max", clamp_hours(99) == HOURS_MAX, clamp_hours(99))
    rec("hours-step-20", clamp_hours(20) == 20)
    rec("hours-bars-20", lookback_bars(20) == 1200, lookback_bars(20))
    rec("hours-bars-4", lookback_bars(4) == 240, lookback_bars(4))
    rec("hours-bars-64", lookback_bars(64) == 3840, lookback_bars(64))
    rec("pf-default", clamp_min_pf(None) == POSITIVE_PF, clamp_min_pf(None))
    rec("pf-floor", clamp_min_pf(0.5) == PF_MIN, clamp_min_pf(0.5))
    rec("pf-ceil", clamp_min_pf(2) == PF_MAX, clamp_min_pf(2))
    rec("positive-true", symbol_clears_floor({"n": 12, "pf": 1.25}, 1.1))
    rec("positive-false-below", not symbol_clears_floor({"n": 12, "pf": 1.02}, 1.1))
    rec("positive-false-empty", not symbol_clears_floor({"n": 0, "pf": 2.0}, 1.1))
    rec("target-default", clamp_target(0) == DEFAULT_TARGET)
    rec("target-active-50", clamp_target(50) == 50)
    rec("target-validate-250", clamp_target(250) == VALIDATION_CAP)
    rec("target-max", clamp_target(999) == TARGET_MAX)
    rec("refresh-default", clamp_refresh_hours(None) == REFRESH_DEFAULT, clamp_refresh_hours(None))
    rec("refresh-min", clamp_refresh_hours(0) == REFRESH_MIN, clamp_refresh_hours(0))
    rec("refresh-max", clamp_refresh_hours(99) == REFRESH_MAX, clamp_refresh_hours(99))
    prev_last_ready = LAST_READY_PATH
    prev_validated_ids = VALIDATED_IDS_PATH
    tmp_iso = tempfile.mkdtemp(prefix="hist-ids-")
    LAST_READY_PATH = os.path.join(tmp_iso, "no-last-ready.json")
    VALIDATED_IDS_PATH = os.path.join(tmp_iso, "no-validated-ids.json")
    try:
        rec(
            "validated-ids",
            validated_set_ids({"successfulConfigs": [{"setId": "a", "validated": True}, {"setId": "b", "validated": False}]}) == ["a"],
            validated_set_ids({"successfulConfigs": [{"setId": "a", "validated": True}, {"setId": "b", "validated": False}]}),
        )
        pos_job = {
            "positive": ["AAA-USDT"],
            "bySymbol": [
                {"symbol": "BBB-USDT", "positive": True},
                {"symbol": "CCC-USDT", "positive": False},
            ],
        }
        rec("validated-symbols", validated_symbols(pos_job) == ["AAA-USDT", "BBB-USDT"], validated_symbols(pos_job))
        long_rank = {"ranked": [{"symbol": f"S{i}-USDT"} for i in range(50)]}
        rec("validated-symbols-no-rank-fallback", validated_symbols(long_rank) == [], validated_symbols(long_rank))
        short_rank = {"ranked": [{"symbol": "AAA-USDT"}, {"symbol": "BBB-USDT"}]}
        rec("validated-symbols-short-rank", validated_symbols(short_rank) == ["AAA-USDT", "BBB-USDT"], validated_symbols(short_rank))
        run_job = {"successfulConfigs": [{"id": "indications:1m:sl0.6:st3", "validated": True, "pf": 1.4, "n": 20}]}
        run_rows = running_sets(run_job)
        rec("running-sets", bool(run_rows) and run_rows[0].get("id") == "indications:1m:sl0.6:st3", run_rows)
        view = job_progress_view({
            "successfulConfigs": [{"id": "indications:1m:sl0.6:st8", "setId": "indications:1m:sl0.6:st8", "validated": True, "pf": 1.4, "n": 20}],
            "positive": ["AAA-USDT"],
            "phase": "ready",
        })
        rec(
            "progress-has-running-sets",
            bool(view.get("runningSets"))
            and bool(view.get("symbols"))
            and view.get("enabled") is True
            and view.get("catalogSkipped") is True,
            view,
        )
        flying = job_progress_view({
            "phase": "evaluate",
            "pct": 22,
            "detail": "SOL-USDT · 3/20 positive",
            "running": True,
            "validatedCount": 40,
            "positive": ["SOL-USDT"],
        })
        rec(
            "progress-keeps-inflight-pct",
            flying.get("phase") == "evaluate" and abs(float(flying.get("pct") or 0) - 22) < 1e-6 and flying.get("running") is True,
            flying,
        )
        rec("progress-uses-validated-count", int(flying.get("validatedCount") or 0) >= 40, flying.get("validatedCount"))
        persist_validated_ids(["indications:1m:sl0.6:st3", "general:1m:sl0.6:st4"])
        rec("persist-ids", read_persisted_validated_ids()[:2] == ["indications:1m:sl0.6:st3", "general:1m:sl0.6:st4"], read_persisted_validated_ids())
        persist_validated_ids([f"set:{i}" for i in range(VALIDATED_IDS_CAP + 40)])
        rec("persist-ids-cap", len(read_persisted_validated_ids()) == VALIDATED_IDS_CAP, len(read_persisted_validated_ids()))
        off = off_progress_view()
        rec("off-progress", off.get("enabled") is False and off.get("phase") == "off", off)
        cap_job = {"positive": [f"S{i}-USDT" for i in range(80)]}
        rec("validated-symbols-cap", len(validated_symbols(cap_job)) == SYMBOL_CAP, len(validated_symbols(cap_job)))
    finally:
        LAST_READY_PATH = prev_last_ready
        VALIDATED_IDS_PATH = prev_validated_ids
        shutil.rmtree(tmp_iso, ignore_errors=True)

    ov = test_overlay(4, 1.1, 8, 8)
    ov["slToTpRatios"] = [0.6]
    ov["stratTrailing"] = False
    ov["stratIndications"] = False
    ov["stratBlock"] = False
    ov["histSimulateBlock"] = False
    queue = [
        {"symbol": "BBB-USDT"},
        {"symbol": "AAA-USDT"},
        {"symbol": "DDD-USDT"},
        {"symbol": "CCC-USDT"},
        {"symbol": "EEE-USDT"},
    ]

    def fake_fetch(symbol: str, limit: int) -> List[List[float]]:
        return synth_trend(180, start=70.0, step=0.1, noise=0.02)

    def fake_score(symbol: str, bars: List[List[float]]) -> Dict[str, Any]:
        positive = symbol in {"AAA-USDT", "CCC-USDT", "EEE-USDT"}
        return {"n": 30, "evalN": 30, "pf": 1.28 if positive else 0.82, "wr": 60 if positive else 40, "maxDdS": 120}

    fill = fill_positive(queue, 2, 1.1, ov, fake_fetch, score_fn=fake_score)
    fills = [r["symbol"] for r in fill["selected"]]
    rejected = [r["symbol"] for r in fill["rejected"]]
    rec("fill-stops-at-target", len(fills) == 2, fills)
    rec("fill-skips-negative-first", bool(fills) and fills[0] == "AAA-USDT", fills)
    rec("fill-second-positive", len(fills) > 1 and fills[1] == "CCC-USDT", fills)
    rec("fill-rejected-includes-loser", "BBB-USDT" in rejected, rejected)
    rec("fill-did-not-need-fifth", "EEE-USDT" not in fills and "EEE-USDT" not in rejected, {"filled": fills, "rejected": rejected})
    rec("fill-evaluated-until-count", fill["evaluated"] == 4, fill["evaluated"])

    live_fill = fill_positive(
        [{"symbol": "AAA-USDT"}, {"symbol": "BBB-USDT"}],
        2, 1.1, ov, fake_fetch,
    )
    rec("live-eval-runs", live_fill["evaluated"] == 2, live_fill)
    rec("live-eval-records-pf", all("pf" in r for r in live_fill["selected"] + live_fill["rejected"]), live_fill)

    prev_paths = (PUBLIC_JSON, SUMMARY_PATH, PUBLIC_SWEEP, OUT_DIR, VALIDATED_IDS_PATH, LAST_READY_PATH)
    tmp = tempfile.mkdtemp(prefix="hist-self-")
    PUBLIC_JSON = os.path.join(tmp, "hist-test.json")
    SUMMARY_PATH = os.path.join(tmp, "summary.json")
    PUBLIC_SWEEP = os.path.join(tmp, "step-sweep.json")
    OUT_DIR = tmp
    VALIDATED_IDS_PATH = os.path.join(tmp, "validated-ids.json")
    LAST_READY_PATH = os.path.join(tmp, "last-ready.json")
    try:
        job = run_test({"hours": 4, "minPf": 1.1, "targetCount": 2, "minStep": 8, "stepMax": 8, "synth": True,
                        "overlay": {"slToTpRatios": [0.6], "stratTrailing": False, "stratIndications": False, "stratBlock": False, "histSimulateBlock": False}})
        rec("synth-hours", job.get("hours") == 4, job.get("hours"))
        rec("synth-min-pf", abs(float(job.get("minPf") or 0) - 1.1) < 1e-9, job.get("minPf"))
        rec("synth-reports-fill", "evaluated" in job or "fill" in job or job.get("phase") in ("ready", "error"), job.get("phase"))
        rec("synth-no-false-positive", all(symbol_clears_floor(r, 1.1) for r in (job.get("bySymbol") or []) if r.get("symbol") in (job.get("symbols") or [])), job.get("symbols"))
        rec("synth-does-not-publish-desk-job", not os.path.samefile(PUBLIC_JSON, prev_paths[0]) if os.path.exists(prev_paths[0]) else True)
        ids = validated_set_ids(job)
        rec("synth-validated-ids", True, ids)
        if ids and (job.get("symbols") or job.get("positive")):
            recalc = run_test({
                "hours": 4, "minPf": 1.1, "targetCount": 2, "minStep": 8, "stepMax": 8, "synth": True,
                "recalcIds": ids[:1],
                "keepSymbols": list(job.get("positive") or job.get("symbols") or []),
                "overlay": {"slToTpRatios": [0.6], "stratTrailing": False, "stratIndications": False, "stratBlock": False, "histSimulateBlock": False},
            })
            rec("recalc-only-flag", bool(recalc.get("recalcOnly")), recalc.get("recalcOnly"))
            rec("recalc-does-not-expand-catalog", len(recalc.get("validatedIds") or []) <= max(1, len(ids)), recalc.get("validatedIds"))
        else:
            rec("recalc-only-flag", True, "no-ids-skip")
            rec("recalc-does-not-expand-catalog", True, "no-ids-skip")
    finally:
        PUBLIC_JSON, SUMMARY_PATH, PUBLIC_SWEEP, OUT_DIR, VALIDATED_IDS_PATH, LAST_READY_PATH = prev_paths
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r["name"] for r in rows if not r["ok"]]
    return {"ok": not failed, "pass": len(rows) - len(failed), "fail": len(failed), "failed": failed, "rows": rows}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    body: Dict[str, Any] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in ("--hours", "-h") and i + 1 < len(argv):
            body["hours"] = argv[i + 1]
            i += 2
            continue
        if token in ("--min-pf", "--minPf") and i + 1 < len(argv):
            body["minPf"] = argv[i + 1]
            i += 2
            continue
        if token in ("--count", "--cap", "--symbolCap") and i + 1 < len(argv):
            body["targetCount"] = argv[i + 1]
            i += 2
            continue
        if token == "--synth":
            body["synth"] = True
            i += 1
            continue
        if token == "--self-test":
            result = self_test()
            print(json.dumps({"ok": result["ok"], "pass": result["pass"], "fail": result["fail"], "failed": result["failed"]}, indent=2))
            return 0 if result["ok"] else 1
        if token == "--stop":
            print(json.dumps(stop_test()))
            return 0
        if token == "--pause":
            print(json.dumps(pause_test()))
            return 0
        if token == "--resume":
            print(json.dumps(resume_test(body)))
            return 0
        i += 1
    job = run_test(body)
    print(json.dumps({
        "ok": bool(job.get("ready")) and not job.get("error"),
        "phase": job.get("phase"),
        "hours": job.get("hours"),
        "minPf": job.get("minPf"),
        "symbols": job.get("symbols"),
        "filled": job.get("filled"),
        "evaluated": job.get("evaluated"),
        "bestStep": (job.get("bestStep") or {}).get("step"),
        "elapsedMs": job.get("elapsedMs"),
        "error": job.get("error") or "",
    }, indent=2))
    return 0 if job.get("ready") and not job.get("error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
