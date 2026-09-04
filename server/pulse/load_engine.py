#!/usr/bin/env python3
"""System-wide load governor for the pulse engine.

Best practices wired in (sliding windows, bounded caches, chunked work,
backpressure, RSS caps, malloc_trim):

- Never keep unbounded dicts/lists in a long-running process.
- Evaluate a priority window each tick (opens → ranked head → rotating tail)
  instead of the full universe.
- Shed non-critical work (extra venues, higher TFs, historic replay, fat
  stats dumps) before touching SL/TP controls or the hot loop.
- Return pages to the OS with gc.collect + malloc_trim under pressure.
- Coordinate hot / warm / hist threads so they do not allocate together.
"""
from __future__ import annotations

import gc
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LEVELS = ("idle", "normal", "busy", "overload", "critical")
LEVEL_RANK = {n: i for i, n in enumerate(LEVELS)}


def rss_mb() -> float:
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * 4096 / 1048576.0
    except Exception:
        return 0.0


def cgroup_memory_limit_mb() -> float:
    """Return this process' effective memory cap when cgroups expose one."""
    paths = []
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                parts = line.strip().split(":", 2)
                if len(parts) == 3:
                    rel = parts[2].lstrip("/")
                    paths.append(os.path.join("/sys/fs/cgroup", rel, "memory.max"))
                    paths.append(os.path.join("/sys/fs/cgroup", rel, "memory", "memory.limit_in_bytes"))
    except Exception:
        pass
    paths.extend(("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"))
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            with open(path) as f:
                raw = f.read().strip()
            if not raw or raw.lower() in ("max", "infinity"):
                continue
            value = float(raw)
            if value > 0:
                return value / 1048576.0
        except Exception:
            continue
    return 0.0


def malloc_trim() -> bool:
    """Give freed heap pages back to the OS. Python GC alone will not."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        return bool(libc.malloc_trim(0))
    except Exception:
        return False


def trim_map(store: Dict[Any, Any], keep: Iterable[Any]) -> int:
    want = set(keep)
    n = 0
    for k in list(store.keys()):
        if k not in want:
            store.pop(k, None)
            n += 1
    return n


def cap_map(store: Dict[Any, Any], max_n: int) -> int:
    extra = len(store) - max(0, int(max_n))
    if extra <= 0:
        return 0
    n = 0
    for k in list(store.keys()):
        if n >= extra:
            break
        store.pop(k, None)
        n += 1
    return n


def prune_ttl(store: Dict[Any, float], now: Optional[float] = None, slack: float = 0.0) -> int:
    """Drop expired timestamp values (value is unix seconds until/at expiry or last-use)."""
    now = now if now is not None else time.time()
    n = 0
    for k, ts in list(store.items()):
        try:
            t = float(ts)
        except Exception:
            store.pop(k, None)
            n += 1
            continue
        if t + slack < now:
            store.pop(k, None)
            n += 1
    return n


def cap_list(rows: List[Any], max_n: int) -> int:
    extra = len(rows) - max(0, int(max_n))
    if extra <= 0:
        return 0
    del rows[:-max(0, int(max_n))]
    return extra


class BoundedSet:
    """Insertion-ordered set with a hard cap. Oldest keys evict first."""

    def __init__(self, maxsize: int = 500) -> None:
        self.maxsize = max(1, int(maxsize))
        self._d: OrderedDict[Any, None] = OrderedDict()

    def add(self, item: Any) -> None:
        if item in self._d:
            self._d.move_to_end(item)
            return
        self._d[item] = None
        while len(self._d) > self.maxsize:
            self._d.popitem(last=False)

    def discard(self, item: Any) -> None:
        self._d.pop(item, None)

    def clear(self) -> None:
        self._d.clear()

    def __contains__(self, item: Any) -> bool:
        return item in self._d

    def __len__(self) -> int:
        return len(self._d)

    def __iter__(self):
        return iter(self._d)


@dataclass
class Budget:
    level: str = "normal"
    rss_mb: float = 0.0
    scan_chunk: int = 16
    kline_batch: int = 8
    hist_chunk: int = 8
    extra_n: int = 6
    lookback: int = 240
    universe_rows: int = 120
    vol1h_batch: int = 10
    tf_5m: bool = True
    tf_15m: bool = True
    extra_sources: bool = True
    hist_run: bool = True
    kline_rest: bool = True
    do_gc: bool = False
    stats_full: bool = True
    warm_s: float = 0.32
    shed: List[str] = field(default_factory=list)


class LoadGovernor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.partial = True
        self.soft_mb = 0.0  # 0 = scale with universe
        self.hard_mb = 0.0
        self.cgroup_mb = cgroup_memory_limit_mb()
        self.level = "normal"
        self.rss = 0.0
        self.rss_peak = 0.0
        self.hot_ms = 0.0
        self.warm_ms = 0.0
        self.hist_busy = False
        self.kline_ban = False
        self.overrun_n = 0
        self.trimmed = 0
        self.gc_n = 0
        self.last_trim = 0.0
        self.last_gc = 0.0
        self.last_budget = Budget()
        self.cursor_ind = 0
        self.cursor_hist = 0
        self.n_sym = 0
        self.n_open = 0
        self.shed: List[str] = []

    def configure(self, ov: Optional[Dict[str, Any]] = None) -> None:
        ov = ov or {}
        self.partial = bool(ov.get("loadPartial", True))
        try:
            if ov.get("rssSoftMb") is not None:
                self.soft_mb = max(40.0, float(ov.get("rssSoftMb") or 0))
            if ov.get("rssHardMb") is not None:
                self.hard_mb = max(60.0, float(ov.get("rssHardMb") or 0))
        except Exception:
            pass

    def soft_limit(self, n_sym: int) -> float:
        if self.soft_mb > 0:
            return self.soft_mb
        base = 90.0 + max(0, n_sym) * 0.35
        if self.cgroup_mb > 0:
            # Leave room for a bounded replay clone and the Python runtime,
            # while allowing a large-symbol lane to use its explicit service
            # budget instead of being classified critical forever.
            return max(base, min(self.cgroup_mb * 0.58, self.cgroup_mb - 160.0))
        return base

    def hard_limit(self, n_sym: int) -> float:
        if self.hard_mb > 0:
            return self.hard_mb
        base = 140.0 + max(0, n_sym) * 0.55
        if self.cgroup_mb > 0:
            return max(base, min(self.cgroup_mb * 0.78, self.cgroup_mb - 160.0))
        return base

    def observe(
        self,
        *,
        n_sym: int = 0,
        n_open: int = 0,
        hot_ms: float = 0.0,
        warm_ms: float = 0.0,
        hist_busy: bool = False,
        kline_ban: bool = False,
        cycle_overrun: bool = False,
        rss_mb: Optional[float] = None,
    ) -> Budget:
        rss = float(rss_mb if rss_mb is not None else globals()["rss_mb"]())
        with self.lock:
            self.n_sym = int(n_sym)
            self.n_open = int(n_open)
            self.hot_ms = float(hot_ms)
            self.warm_ms = float(warm_ms)
            self.hist_busy = bool(hist_busy)
            self.kline_ban = bool(kline_ban)
            self.rss = rss
            if rss > self.rss_peak:
                self.rss_peak = rss
            if cycle_overrun:
                self.overrun_n += 1
            else:
                self.overrun_n = max(0, self.overrun_n - 1)
            b = self._compute(cycle_overrun=cycle_overrun)
            self.last_budget = b
            self.level = b.level
            self.shed = list(b.shed)
            return b

    def _compute(self, cycle_overrun: bool = False) -> Budget:
        n = max(0, self.n_sym)
        n_open = max(0, self.n_open)
        rss = self.rss
        soft = self.soft_limit(n)
        hard = self.hard_limit(n)
        crit = hard + 80.0
        shed: List[str] = []

        if not self.partial:
            return Budget(
                level="normal",
                rss_mb=round(rss, 1),
                scan_chunk=max(1, n or 16),
                kline_batch=8,
                hist_chunk=max(4, min(16, n or 8)),
                extra_n=6,
                lookback=240,
                universe_rows=120,
                vol1h_batch=10,
                tf_5m=True,
                tf_15m=True,
                extra_sources=True,
                hist_run=True,
                kline_rest=True,
                do_gc=False,
                stats_full=n <= 48,
                warm_s=0.32,
            )

        level = "idle"
        if rss >= crit or (cycle_overrun and rss >= hard) or self.overrun_n >= 8:
            level = "critical"
        elif rss >= hard or cycle_overrun or (self.hist_busy and self.warm_ms > 420):
            level = "overload"
        elif rss >= soft or self.warm_ms > 280 or self.hot_ms > 180 or n > 80:
            level = "busy"
        elif n > 0:
            level = "normal"

        b = Budget(level=level, rss_mb=round(rss, 1))
        if level == "critical":
            b.scan_chunk = max(4, min(8, n_open + 4))
            b.kline_batch = 2
            b.hist_chunk = 2
            b.extra_n = 0
            b.lookback = 80
            b.universe_rows = 40
            b.vol1h_batch = 3
            b.tf_5m = False
            b.tf_15m = False
            b.extra_sources = False
            b.hist_run = False
            b.kline_rest = not self.hist_busy
            b.do_gc = True
            b.stats_full = False
            b.warm_s = 0.55
            shed = ["extra", "tf15m", "tf5m", "hist", "fat-stats"]
        elif level == "overload":
            b.scan_chunk = max(6, min(12, 8 + n_open))
            b.kline_batch = 3
            b.hist_chunk = 3
            b.extra_n = 2
            b.lookback = 120
            b.universe_rows = 60
            b.vol1h_batch = 4
            b.tf_5m = True
            b.tf_15m = False
            b.extra_sources = False
            b.hist_run = rss < hard
            b.kline_rest = not self.hist_busy
            b.do_gc = True
            b.stats_full = False
            b.warm_s = 0.48
            shed = ["extra", "tf15m", "fat-stats"]
            if not b.hist_run:
                shed.append("hist")
        elif level == "busy":
            b.scan_chunk = max(8, min(16, 10 + min(n_open, 8)))
            b.kline_batch = 3
            b.hist_chunk = 4 if n > 200 else 6
            b.extra_n = 2 if n > 80 else 4
            b.lookback = 120 if n > 200 else 180
            b.universe_rows = 60 if n > 200 else 80
            b.vol1h_batch = 4
            b.tf_5m = True
            b.tf_15m = n <= 80
            b.extra_sources = n <= 40
            b.hist_run = True
            b.kline_rest = not (self.hist_busy and n > 48)
            b.do_gc = rss >= soft or n > 200
            b.stats_full = n <= 40
            b.warm_s = 0.45
            if not b.tf_15m:
                shed.append("tf15m")
            if not b.extra_sources:
                shed.append("extra")
            if not b.stats_full:
                shed.append("fat-stats")
        else:
            b.scan_chunk = max(12, min(32, n if n <= 32 else 24))
            b.kline_batch = 8
            b.hist_chunk = 10
            b.extra_n = 6
            b.lookback = 240
            b.universe_rows = 120
            b.vol1h_batch = 10
            b.tf_5m = True
            b.tf_15m = True
            b.extra_sources = True
            b.hist_run = True
            b.kline_rest = True
            b.do_gc = False
            b.stats_full = n <= 48
            b.warm_s = 0.32
            if not b.stats_full:
                shed.append("fat-stats")

        if n:
            b.scan_chunk = min(b.scan_chunk, n)
            b.hist_chunk = min(b.hist_chunk, n)
            b.kline_batch = min(b.kline_batch, n)
        if self.kline_ban:
            b.kline_rest = False
            b.extra_sources = False
            shed.append("kline-ban")
        b.shed = shed
        return b

    def scan_window(
        self,
        symbols: Sequence[str],
        open_syms: Sequence[str],
        chunk: int,
        cursor: int,
        ranked: Optional[Sequence[str]] = None,
    ) -> Tuple[List[str], int]:
        names = [s for s in symbols if s]
        if not names:
            return [], 0
        take = max(1, int(chunk or 1))
        if take >= len(names) or not self.partial:
            return list(names), 0
        out: List[str] = []
        seen = set()
        name_set = set(names)
        for s in open_syms:
            if s and s in name_set and s not in seen:
                out.append(s)
                seen.add(s)
        remaining = max(0, take - len(out))
        ranked_n = min(4, remaining // 2) if remaining >= 2 else remaining
        head_src = list(ranked or names)
        added = 0
        for s in head_src:
            if added >= ranked_n:
                break
            if s and s not in seen:
                out.append(s)
                seen.add(s)
                added += 1
        rot = [s for s in names if s not in seen]
        if not rot:
            return out[:take], 0
        n = len(rot)
        start = int(cursor) % n
        need = max(0, take - len(out))
        for i in range(need):
            out.append(rot[(start + i) % n])
        nxt = (start + need) % n
        return out[:take], nxt

    def free(self, force: bool = False) -> Dict[str, Any]:
        now = time.time()
        if not force and now - self.last_gc < 6.0:
            return {"skipped": True, "rssMb": round(self.rss, 1)}
        before = rss_mb()
        try:
            n0 = gc.collect(0)
            n1 = gc.collect(1)
            n2 = gc.collect(2)
        except Exception:
            n0 = n1 = n2 = 0
        trimmed = malloc_trim()
        after = rss_mb()
        self.last_gc = now
        self.gc_n += 1
        self.rss = after
        return {
            "skipped": False,
            "rssBefore": round(before, 1),
            "rssAfter": round(after, 1),
            "collected": n0 + n1 + n2,
            "trim": trimmed,
        }

    def snapshot(self) -> Dict[str, Any]:
        b = self.last_budget
        return {
            "level": self.level,
            "rssMb": round(self.rss, 1),
            "peakMb": round(self.rss_peak, 1),
            "softMb": round(self.soft_limit(self.n_sym), 1),
            "hardMb": round(self.hard_limit(self.n_sym), 1),
            "cgroupMb": round(self.cgroup_mb, 1),
            "scanChunk": int(b.scan_chunk),
            "histChunk": int(b.hist_chunk),
            "klineBatch": int(b.kline_batch),
            "lookback": int(b.lookback),
            "extraN": int(b.extra_n),
            "tf5m": bool(b.tf_5m),
            "tf15m": bool(b.tf_15m),
            "extraSources": bool(b.extra_sources),
            "histRun": bool(b.hist_run),
            "doGc": bool(b.do_gc),
            "statsFull": bool(b.stats_full),
            "partial": bool(self.partial),
            "trimmed": int(self.trimmed),
            "gcN": int(self.gc_n),
            "overrunN": int(self.overrun_n),
            "shed": list(self.shed)[:8],
            "hotMs": round(self.hot_ms, 1),
            "warmMs": round(self.warm_ms, 1),
            "histBusy": bool(self.hist_busy),
        }


def self_test() -> List[Tuple[str, bool, str]]:
    out: List[Tuple[str, bool, str]] = []
    g = LoadGovernor()
    b = g.observe(n_sym=12, n_open=1, hot_ms=40, warm_ms=70, rss_mb=42.0)
    out.append(("load-level-calm", b.level in ("idle", "normal"), f"level={b.level} chunk={b.scan_chunk}"))
    out.append(("load-chunk-fits", b.scan_chunk >= 8 and b.scan_chunk <= 12, f"chunk={b.scan_chunk}"))
    out.append(("load-tf-calm", b.tf_5m and b.tf_15m and b.hist_run, f"5m={b.tf_5m} 15m={b.tf_15m}"))
    b2 = g.observe(
        n_sym=400,
        n_open=3,
        hot_ms=320,
        warm_ms=520,
        rss_mb=520.0,
        cycle_overrun=True,
        hist_busy=True,
    )
    out.append(("load-level-hot", b2.level in ("overload", "critical"), f"level={b2.level} rss={b2.rss_mb}"))
    out.append(("load-shed-hot", (not b2.tf_15m) and (not b2.extra_sources) and b2.scan_chunk <= 12, f"chunk={b2.scan_chunk} shed={b2.shed}"))
    out.append(("load-backpressure", b2.kline_rest is False or "hist" in b2.shed or not b2.hist_run, f"klineRest={b2.kline_rest} hist={b2.hist_run}"))
    keep, cur = g.scan_window(["A", "B", "C", "D", "E", "F"], ["C"], chunk=3, cursor=0)
    out.append(("load-window-open-first", keep[:1] == ["C"] and len(keep) == 3, f"keep={keep} cur={cur}"))
    keep2, cur2 = g.scan_window(["A", "B", "C", "D", "E", "F"], ["C"], chunk=3, cursor=cur)
    out.append(("load-window-rotate", keep2[0] == "C" and keep2 != keep and cur2 != cur, f"keep2={keep2} cur2={cur2}"))
    d = {"A": 1, "B": 2, "C": 3, "D": 4}
    n = trim_map(d, {"A", "C"})
    out.append(("load-trim-map", n == 2 and set(d) == {"A", "C"}, f"n={n} keys={sorted(d)}"))
    n2 = cap_map(d, 1)
    out.append(("load-cap-map", n2 == 1 and len(d) == 1, f"n={n2} keys={list(d)}"))
    ttl = {"x": time.time() - 5, "y": time.time() + 30}
    n3 = prune_ttl(ttl)
    out.append(("load-prune-ttl", n3 == 1 and "y" in ttl and "x" not in ttl, f"n={n3} {list(ttl)}"))
    s = BoundedSet(3)
    for ch in "abcdef":
        s.add(ch)
    out.append(("load-bounded-set", len(s) == 3 and "f" in s and "a" not in s, f"n={len(s)} {list(s)}"))
    s.add("f")
    out.append(("load-bounded-touch", "f" in s, "ok"))
    rows = list(range(10))
    dropped = cap_list(rows, 4)
    out.append(("load-cap-list", dropped == 6 and rows == [6, 7, 8, 9], f"drop={dropped} {rows}"))
    snap = g.snapshot()
    out.append(("load-snap", snap.get("level") == b2.level and "scanChunk" in snap, str(snap.get("level"))))
    freed = g.free(force=True)
    out.append(("load-free", freed.get("skipped") is False and "rssAfter" in freed, str(freed)))
    g.configure({"loadPartial": False})
    b3 = g.observe(n_sym=200, n_open=0, rss_mb=40.0)
    out.append(("load-partial-off", b3.scan_chunk == 200 and b3.tf_15m, f"chunk={b3.scan_chunk}"))
    g.configure({"loadPartial": True})
    ranks = [LEVEL_RANK[x] for x in LEVELS]
    out.append(("load-levels", ranks == list(range(5)), str(LEVELS))
    )
    return out


if __name__ == "__main__":
    failed = 0
    for name, ok, detail in self_test():
        print(("PASS" if ok else "FAIL"), name, detail)
        failed += int(not ok)
    if failed:
        raise SystemExit(1)
    print("load_engine ok")
