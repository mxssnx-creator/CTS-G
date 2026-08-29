#!/usr/bin/env python3
"""Intense VST x02 stress: CPU/mem, replay slice, stats dump, public API burst."""
from __future__ import annotations

import gc
import json
import os
import time
import traceback
from typing import Any, Dict, List, Tuple

DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIR)

from set_engine import SetBook, synth_trend, self_test as sets_self_test
from exit_engine import self_test as exit_self_test
from indication_engine import self_test as indication_self_test
from risk_variants import self_test as variants_self_test
from position_cost import last_n_cost_pf, signed_result_r, ratio_from_r


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def rec(name: str, ok: bool, detail: str, out: List[Tuple[str, bool, str]]) -> None:
    out.append((name, ok, detail))


def stress_cpu_mem(out: List[Tuple[str, bool, str]]) -> None:
    gc.collect()
    r0 = rss_mb()
    t0 = time.perf_counter()
    book = SetBook()
    book.load({"histLookbackBars": 240, "setMinSamples": 5, "setMaxActive": 12, "exitIgnoreTp": True})
    for i, px in enumerate((40.0, 12.0, 8.0, 3.0, 1.2, 0.4)):
        book.ingest_bars(f"S{i}-USDT", synth_trend(240, px, 0.12 if i % 2 == 0 else -0.1, 0.04))
    times = []
    for n in range(6):
        t1 = time.perf_counter()
        steps = {"n": 0}

        def step() -> None:
            steps["n"] += 1
            time.sleep(0.0)

        book.replay_all(on_step=step)
        times.append((time.perf_counter() - t1) * 1000)
    r1 = rss_mb()
    rec("stress-replay-6x", max(times) < 2500 and min(times) > 0, f"ms={['%.0f'%x for x in times]} rssΔ={r1-r0:.1f}MB", out)
    rec("stress-replay-yield", steps["n"] >= 4, f"steps={steps['n']}", out)

    t2 = time.perf_counter()
    tape = [{"t": 1000 + i, "pnl": (0.02 if i % 3 else -0.01), "pnl_pct": (0.003 if i % 3 else -0.002)} for i in range(80)]
    for _ in range(4000):
        last_n_cost_pf(tape, 15, 0.15)
        signed_result_r(0.003, 0.15)
        ratio_from_r(1.0)
    pf_ms = (time.perf_counter() - t2) * 1000
    rec("stress-pf-4k", pf_ms < 400, f"{pf_ms:.1f}ms", out)

    snap_ms = []
    blob_n = 0
    for _ in range(200):
        t3 = time.perf_counter()
        blob = json.dumps(book.snapshot(), separators=(",", ":"))
        blob_n = len(blob)
        snap_ms.append((time.perf_counter() - t3) * 1000)
    rec("stress-snap-200", sorted(snap_ms)[100] < 8.0, f"p50={sorted(snap_ms)[100]:.2f}ms bytes={blob_n} rows={len(book.snapshot().get('rows') or [])}", out)

    r2 = rss_mb()
    rec("stress-rss-cap", r2 < 90 and (r2 - r0) < 25, f"start={r0:.1f} end={r2:.1f} Δ={r2-r0:.1f}", out)
    rec("stress-wall", (time.perf_counter() - t0) < 20, f"{(time.perf_counter()-t0):.2f}s", out)


def stress_unit(out: List[Tuple[str, bool, str]]) -> None:
    for fn, tag in ((sets_self_test, "set"), (exit_self_test, "ex"), (variants_self_test, "var"), (indication_self_test, "ind")):
        try:
            rows = fn()
        except Exception as e:
            rec(f"stress-{tag}-exc", False, str(e)[:160], out)
            continue
        bad = [r for r in rows if not r[1]]
        rec(f"stress-{tag}-suite", not bad, f"n={len(rows)} fail={len(bad)}", out)


def stress_api(out: List[Tuple[str, bool, str]]) -> None:
    try:
        from pulse_trader import redis_hget, FastBingX, ErrorLog
    except Exception:
        from bingx_fast import FastBingX, ErrorLog

        def redis_hget(field: str) -> str:
            import subprocess
            conn = os.environ.get("PULSE_CONN", "bingx-x02")
            r = subprocess.run(["redis-cli", "hget", f"connection:{conn}", field], capture_output=True, text=True)
            return (r.stdout or "").strip()

    key = redis_hget("api_key")
    secret = redis_hget("api_secret")
    if not key or not secret:
        rec("stress-api-keys", False, "missing redis creds", out)
        return
    rec("stress-api-keys", True, "ok", out)
    base = redis_hget("base_url") or "https://open-api-vst.bingx.com"
    err = ErrorLog("/tmp/stress-errors.jsonl")
    api = FastBingX(key, secret, err, base=base)
    t0 = time.perf_counter()
    tick = api.public("/openApi/swap/v2/quote/ticker")
    dt = (time.perf_counter() - t0) * 1000
    rows = tick.get("data") or []
    rec("stress-ticker", isinstance(rows, list) and len(rows) > 20, f"{dt:.0f}ms n={len(rows)} code={tick.get('code')}", out)
    syms = ["SOL-USDT", "XRP-USDT", "DOGE-USDT", "APT-USDT", "TRX-USDT", "ETC-USDT", "ENA-USDT", "LDO-USDT"]
    reqs = [("/openApi/swap/v3/quote/klines", {"symbol": s, "interval": "1m", "limit": "60"}) for s in syms]
    t1 = time.perf_counter()
    got = api.gather_public(reqs, timeout=6.0)
    gms = (time.perf_counter() - t1) * 1000
    ok_n = sum(1 for _p, _e, b in got if isinstance(b, dict) and (b.get("data") or b.get("code") == 0 or isinstance(b.get("data"), list)))
    rec("stress-klines-batch8", ok_n >= 6 and gms < 2500, f"{gms:.0f}ms ok={ok_n}/{len(reqs)} p50={api.stats.get('asyncP50')}", out)
    t2 = time.perf_counter()
    waits = 0.0
    for _ in range(12):
        api.public("/openApi/swap/v2/quote/ticker")
    burst = (time.perf_counter() - t2) * 1000
    rec("stress-rate-burst12", burst < 8000, f"{burst:.0f}ms waits={api.stats.get('wait',0):.2f} rl={api.stats.get('rl',0)}", out)
    rec("stress-err-quiet", err.n < 40, f"errN={err.n}", out)


def main() -> int:
    os.environ.setdefault("PULSE_CONN", "bingx-x02")
    out: List[Tuple[str, bool, str]] = []
    print("stress vst start rss=%.1fMB" % rss_mb())
    stress_unit(out)
    stress_cpu_mem(out)
    try:
        stress_api(out)
    except Exception:
        rec("stress-api-exc", False, traceback.format_exc()[-220:], out)
    failed = 0
    for name, ok, detail in out:
        print(("PASS" if ok else "FAIL"), name, detail)
        failed += int(not ok)
    rss = rss_mb()
    print("stress vst done rss=%.1fMB fail=%d/%d" % (rss, failed, len(out)))
    try:
        json.dump(
            {"rssMb": rss, "fail": failed, "n": len(out), "rows": [{"name": n, "pass": ok, "detail": d} for n, ok, d in out]},
            open("/tmp/stress-vst.json", "w"),
            indent=2,
        )
    except Exception:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
