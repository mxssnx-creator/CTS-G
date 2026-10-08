"""Positive-processing suite: every configured Set is processed, validated and kept processing.

Covers: all Sets scored on a full refresh; trades produced and every trade valid; continuity across
refreshes; no Set stuck after a failure; failure isolation per symbol and per Set; chunked refreshes
equal one full refresh; live closes counted once and gate recomputed; replay deterministic; and
replay and live-close paths safe under concurrency.
"""
from __future__ import annotations

import math
import threading
import time

from common import COST, SMALL, T0, Skip, overlay, synth_bars  # noqa: F401
from set_engine import BAR_S, SetBook

VALID_REASONS = {"sl", "tp", "time", "scratch+"}


def _book(grid_extra=None, n_syms=4, bars=1200, seed=7, **extra):
    b = SetBook()
    ov = overlay(**extra) if not grid_extra else {**overlay(), **grid_extra}
    b.load(ov)
    for i in range(n_syms):
        sym = f"S{i}-USDT"
        b.bars[sym] = synth_bars(seed + i, bars, drift=0.0004 if i % 2 == 0 else -0.0004)
    return b


def _symbols(b):
    return sorted(b.bars)


def every_set_processed_full_grid():
    b = _book(n_syms=4, bars=1200)
    b.replay_all(now=T0)
    p = b.progress
    unscored = [st.id for st in b.by_idx if st.last_error or st.gate_n < 0]
    ok = p.phase == "ready" and p.ready and p.sets_done == len(b.by_idx) == 1174 and not unscored and p.errors == 0
    return ok, f"phase={p.phase} sets={len(b.by_idx)} sets_done={p.sets_done} unscored={len(unscored)} errors={p.errors}"


def trades_produced_and_positive_signal():
    b = _book(n_syms=4, bars=1200)
    b.replay_all(now=T0)
    trades = sum(len(v) for v in [st.hist for st in b.by_idx])
    with_trades = sum(1 for st in b.by_idx if st.n > 0)
    ok = with_trades > 0 and trades > 0
    return ok, f"sets_with_trades={with_trades} of {len(b.by_idx)} tape_rows={trades}"


def trade_invariants_hold():
    b = _book(n_syms=4, bars=1200)
    b.replay_all(now=T0)
    bad = []
    rows = 0
    for st in b.by_idx:
        for r in st.hist:
            rows += 1
            m = []
            if r.get("reason") not in VALID_REASONS: m.append("reason")
            if not math.isfinite(r["pnl"]) or not math.isfinite(r["pnl_pct"]): m.append("finite")
            if abs(r["pnl"] - (r["pnl_pct"] - COST / 100.0)) > 1e-12: m.append("net")
            if r.get("hold_s", 0) < BAR_S: m.append("hold")
            if r.get("side") not in ("LONG", "SHORT"): m.append("side")
            if m: bad.append((st.id, m))
    ok = rows > 0 and not bad
    return ok, f"rows={rows} violations={len(bad)} {bad[:2]}"


def deterministic_replay():
    a = _book(n_syms=3, bars=900)
    c = _book(n_syms=3, bars=900)
    a.replay_all(now=T0)
    c.replay_all(now=T0)
    same = all(x.hist == y.hist and x.n == y.n for x, y in zip(a.by_idx, c.by_idx))
    return same, f"sets={len(a.by_idx)} identical={same}"


def continuous_across_refreshes():
    b = _book(n_syms=3, bars=900, **SMALL)
    b.replay_all(now=T0)
    cyc1 = b.progress.cycle
    last1 = b.last_run
    sym = _symbols(b)[0]
    b.bars[sym] = b.bars[sym] + synth_bars(99, 60, start=b.bars[sym][-1][3])
    b.replay_all(now=T0 + 60 * BAR_S)
    p = b.progress
    stuck = [st.id for st in b.by_idx if st.last_error]
    ok = p.phase == "ready" and p.cycle == cyc1 + 1 and b.last_run >= last1 and not stuck and p.sets_done == len(b.by_idx)
    return ok, f"cycle {cyc1}->{p.cycle} phase={p.phase} stuck={len(stuck)}"


def due_cadence():
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    right_after = b.due()
    b.last_run = time.time() - b.refresh_s - 1
    later = b.due()
    b._running = True
    while_running = b.due()
    b._running = False
    ok = right_after is False and later is True and while_running is False
    return ok, f"just_ran={right_after} after_cadence={later} while_running={while_running}"


def symbol_failure_isolated_and_recovers():
    b = _book(n_syms=4, bars=1200)
    good = b.bars["S1-USDT"]
    b.bars["S1-USDT"] = [["x", "x", "x", "x", "x"]] * len(good)
    b.replay_all(now=T0)
    failed_phase = b.progress.phase
    others_ok = all(sym in b._hist_rows for sym in ("S0-USDT", "S2-USDT", "S3-USDT")) and "S1-USDT" not in b._hist_rows
    sets_scored = sum(1 for st in b.by_idx if st.n > 0)
    b.bars["S1-USDT"] = good
    b.replay_all(now=T0 + BAR_S)
    recovered = b.progress.phase == "ready" and b.progress.errors == 0 and "S1-USDT" in b._hist_rows
    ok = failed_phase == "ready" and others_ok and sets_scored > 0 and recovered
    return ok, f"phase_after_failure={failed_phase} others_kept={others_ok} sets_scored={sets_scored} recovered={recovered}"


def set_failure_isolated():
    b = _book(n_syms=3, bars=900)
    victim = b.by_idx[5].id
    orig = b._score_one

    def flaky(st, now=None):
        if st.id == victim:
            raise RuntimeError("injected")
        return orig(st, now=now)

    b._score_one = flaky
    b.replay_all(now=T0)
    others_clean = all(st.last_error == "" for st in b.by_idx if st.id != victim)
    victim_marked = b.sets[victim].last_error != ""
    ok = others_clean and victim_marked and b.progress.phase == "ready" and b.progress.errors >= 1
    return ok, f"others_clean={others_clean} victim_marked={victim_marked} errors={b.progress.errors}"


def chunked_refresh_equals_full():
    full = _book(n_syms=4, bars=900, **SMALL)
    chunk = _book(n_syms=4, bars=900, **SMALL)
    syms = _symbols(full)
    full.replay_all(now=T0)
    chunk.replay_all(now=T0, symbols=syms[:2])
    chunk.replay_all(now=T0, symbols=syms[2:])
    same = all(
        x.n == y.n and round(x.gate_pf, 9) == round(y.gate_pf, 9) and x.hist == y.hist
        for x, y in zip(full.by_idx, chunk.by_idx)
    )
    return same, f"sets={len(full.by_idx)} equal={same}"


def live_close_counted_once_and_gate_updates():
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    st = b.by_idx[0]
    before = len(st.live)
    rec = {"set_id": st.id, "t": T0 + 5000, "symbol": "L-USDT", "side": "LONG", "pnl": 0.25, "pnl_pct": 0.0025,
           "hold_s": 60, "reason": "tp", "client_id": "cid-1"}
    b.on_live_close(rec)
    b.on_live_close(dict(rec))                 # the same close delivered twice
    b.on_live_close({"set_id": st.id, "t": T0 + 5100, "symbol": "L-USDT", "side": "LONG", "pnl": 1, "reason": "tp"})  # no pnl_pct
    added = len(st.live) - before
    ok = added == 1 and b.live_skipped >= 1 and abs(st.live[-1]["pnl"] - (0.0025 - COST / 100.0)) < 1e-12
    return ok, f"added={added} skipped={b.live_skipped} live_unit_net={st.live[-1]['pnl']:.6f}"


def concurrent_replay_and_live_closes():
    b = _book(n_syms=3, bars=900, **SMALL)
    b.replay_all(now=T0)
    errors = []
    stop = threading.Event()

    def replays():
        try:
            for k in range(3):
                b.replay_all(now=T0 + (k + 1) * 60)
        except Exception as exc:  # noqa: BLE001
            errors.append(("replay", repr(exc)))
        finally:
            stop.set()

    def closes():
        try:
            k = 0
            while not stop.is_set():
                st = b.by_idx[k % len(b.by_idx)]
                b.on_live_close({"set_id": st.id, "t": T0 + 9000 + k, "symbol": "C-USDT", "side": "LONG",
                                 "pnl": 0.1, "pnl_pct": 0.001, "hold_s": 60, "reason": "tp", "client_id": f"c{k}"})
                b.pick("general")
                k += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(("closes", repr(exc)))

    t1 = threading.Thread(target=replays)
    t2 = threading.Thread(target=closes)
    t1.start(); t2.start()
    t1.join(120); t2.join(120)
    alive = t1.is_alive() or t2.is_alive()
    all_scored = all(st.last_error == "" for st in b.by_idx)
    ok = not errors and not alive and all_scored
    return ok, f"errors={errors[:2]} alive={alive} all_scored={all_scored}"


CHECKS = [
    ("processing.all-sets-processed-full-grid", every_set_processed_full_grid),
    ("processing.trades-produced", trades_produced_and_positive_signal),
    ("processing.trade-invariants", trade_invariants_hold),
    ("processing.deterministic-replay", deterministic_replay),
    ("processing.continuous-across-refreshes", continuous_across_refreshes),
    ("processing.due-cadence", due_cadence),
    ("processing.symbol-failure-isolated-and-recovers", symbol_failure_isolated_and_recovers),
    ("processing.set-failure-isolated", set_failure_isolated),
    ("processing.chunked-refresh-equals-full", chunked_refresh_equals_full),
    ("processing.live-close-once-and-gate", live_close_counted_once_and_gate_updates),
    ("processing.concurrent-replay-and-live-closes", concurrent_replay_and_live_closes),
]
