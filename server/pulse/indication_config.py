"""Range configuration of the additional indications: families x ranges x timeframes x relations, each with its own distances.

The overlay key `indicationConfigs` is a list of entries. One entry names indicator FAMILIES of the library and the ranges, timeframes and
relations to combine; every combination that the library knows becomes one indicator name (and so one pack with its own Sets and its own Base
validation, as every name in `extraIndicators` already is). The same entry says how those packs' Sets are built (the TP-step range, the SL ratios,
the types) and, optionally, the rest after a winning close of those indications.

  {"label": "trend fast", "enabled": true,
   "families": ["trend", "donch"],          library families (the part of a name before _n<range>_t<tf>, _c<range> or _x<range>)
   "names": ["g_trend_min"],                 or exact names (group votes, any composite), added as they are
   "ranges": [5, 8, 14],                     -> _n5, _n8, _n14 (the range of the family)
   "timeframes": [1, 5, "c"],                -> _t1, _t5 (one series), "c" the vote over 1/5/15, "x" the vote over 1/5/15/30
   "relations": ["", "T5", "B15"],           "" = none; else name@relation (indication_rel), applied to every name above
   "stepMin": 3, "stepMax": 10,              TP steps (one step = 0.1 % of price) of this entry's Sets
   "slRatios": [1.0, 1.5],                   SL ratios of this entry's Sets (default: the global grid)
   "types": ["base", "exit", "trail"],       which Set types this entry builds (default all)
   "trailStepMin": 4, "trailStepMax": 8,     the trailing arms of this entry's trailing Sets (steps of 0.1 %; default: every arm up to stepMax)
   "pause": "auto" | 0 | 15}                 rest after a winning close: the book's pause mode (default), none, or fixed minutes

Nothing here relaxes a gate: the packs are judged by the same Base -> Main -> Real chain as every other pack. The expansion is bounded
(MAX_NAMES_PER_ENTRY, MAX_NAMES_TOTAL) so a setting cannot ask for an unbounded Set count; a combination over the bound is dropped and listed."""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

KINDS = ("base", "exit", "trail")
TIMEFRAMES = (1, 5, 15, 30)
COMBINED = {"c": "c", "x": "x"}          # _c<range>: vote over 1/5/15, _x<range>: over 1/5/15/30
MAX_NAMES_PER_ENTRY = 128
MAX_NAMES_TOTAL = 384
MAX_ENTRIES = 64


def _ints(raw: Any, lo: int, hi: int) -> List[int]:
    if isinstance(raw, str):
        raw = [x for x in raw.replace(";", ",").split(",") if x.strip()]
    out: List[int] = []
    for x in raw or []:
        try:
            v = int(round(float(x)))
        except Exception:
            continue
        if lo <= v <= hi and v not in out:
            out.append(v)
    return out


def _words(raw: Any) -> List[str]:
    if isinstance(raw, str):
        raw = [x for x in raw.replace(";", ",").split(",")]
    return [str(x).strip() for x in (raw or []) if str(x).strip()]


def suffixes(ranges: Sequence[int], timeframes: Sequence[Any]) -> List[str]:
    """The name suffixes of a range/timeframe grid, in a stable order: _n<r>_t<tf> for a single series, _c<r> / _x<r> for the combined votes."""
    out: List[str] = []
    for r in ranges:
        for tf in timeframes:
            if isinstance(tf, str):
                if tf in COMBINED:
                    out.append(f"_{tf}{r}")
            elif int(tf) in TIMEFRAMES:
                out.append(f"_n{r}_t{int(tf)}")
    return out


def expand(configs: Any, known: Callable[[str], bool], step_lo: int, step_hi: int,
           snap_ratio: Callable[[float], float], default_ratios: Sequence[float]) -> Dict[str, Any]:
    """Expand `indicationConfigs` into {"names", "ranges", "pause", "dropped", "entries"}.

    names    the indicator names to run as packs (sorted, unique)
    ranges   name -> {"lo", "hi", "ratios", "kinds"}: how that name's Sets are built (the last entry naming it wins)
    pause    name -> minutes for entries that fix the pause (0 = none); entries on "auto" are absent (the book's mode decides)
    dropped  [{"entry", "name", "reason"}]: combinations the library does not know, or that exceed the bounds
    entries  one summary per entry: its label, how many names and Sets-grid dimensions it produced"""
    names: Dict[str, None] = {}
    ranges: Dict[str, Dict[str, Any]] = {}
    pause: Dict[str, int] = {}
    dropped: List[Dict[str, str]] = []
    entries: List[Dict[str, Any]] = []
    if not isinstance(configs, (list, tuple)):
        return {"names": [], "ranges": {}, "pause": {}, "dropped": [], "entries": []}
    for e_i, raw in enumerate(list(configs)[:MAX_ENTRIES]):
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        label = str(raw.get("label") or f"entry {e_i + 1}")[:60]
        fam = _words(raw.get("families"))
        exact = _words(raw.get("names"))
        rngs = _ints(raw.get("ranges"), 2, 60)
        tfs: List[Any] = []
        for x in (raw.get("timeframes") if isinstance(raw.get("timeframes"), (list, tuple)) else _words(raw.get("timeframes"))):
            sx = str(x).strip().lower()
            if sx in COMBINED:
                tfs.append(sx)
            else:
                v = _ints([x], 1, 30)
                if v and v[0] in TIMEFRAMES:
                    tfs.append(v[0])
        rr = raw.get("relations")
        if isinstance(rr, str):
            rr = rr.replace(";", ",").split(",")
        rels: List[str] = []
        for r in (rr if rr else [""]):
            r = str(r).strip()
            r = "" if r.lower() in ("", "none") else r      # an empty entry (or "none") is the name without a relation
            if r not in rels:
                rels.append(r)
        base_names = list(exact)
        if fam:
            for f in fam:
                for suf in suffixes(rngs or [8], tfs or [1]):
                    base_names.append(f + suf)
        lo = max(step_lo, min(step_hi, int(raw.get("stepMin") or step_lo)))
        hi = max(lo, min(step_hi, int(raw.get("stepMax") or step_hi)))
        ratios: List[float] = []
        for x in raw.get("slRatios") or default_ratios:
            try:
                v = snap_ratio(float(x))
            except Exception:
                continue
            if math.isfinite(v) and v not in ratios:
                ratios.append(v)
        kinds = [k for k in KINDS if k in (_words(raw.get("types")) or list(KINDS))] or list(KINDS)
        t_lo = _ints([raw.get("trailStepMin")], 2, 50) if raw.get("trailStepMin") not in (None, "") else []
        t_hi = _ints([raw.get("trailStepMax")], 2, 50) if raw.get("trailStepMax") not in (None, "") else []
        trail_lo = t_lo[0] if t_lo else 0
        trail_hi = max(trail_lo, t_hi[0]) if t_hi else 0
        pz = raw.get("pause", "auto")
        made = 0
        for base in base_names:
            for rel in rels:
                name = f"{base}@{rel}" if rel else base
                if name in names and name in ranges and ranges[name].get("entry") == e_i:
                    continue
                if made >= MAX_NAMES_PER_ENTRY or (len(names) >= MAX_NAMES_TOTAL and name not in names):
                    dropped.append({"entry": label, "name": name, "reason": "over the bound"})
                    continue
                if not known(name):
                    dropped.append({"entry": label, "name": name, "reason": "unknown to the library"})
                    continue
                names[name] = None
                ranges[name] = {"lo": lo, "hi": hi, "ratios": sorted(set(ratios)) or list(default_ratios), "kinds": kinds, "entry": e_i,
                               "trailLo": trail_lo, "trailHi": trail_hi}
                if isinstance(pz, (int, float)) and not isinstance(pz, bool):
                    pause[name] = max(0, int(pz))
                else:
                    pause.pop(name, None)
                made += 1
        entries.append({"label": label, "names": made, "stepMin": lo, "stepMax": hi, "ratios": len(ratios), "types": kinds,
                        "steps": hi - lo + 1, "trailStepMin": trail_lo or None, "trailStepMax": trail_hi or None})
    return {"names": sorted(names), "ranges": ranges, "pause": pause, "dropped": dropped, "entries": entries}
