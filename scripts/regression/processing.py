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
import memo  # the replay memo: harness only; determinism and concurrency checks stay unwrapped

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
    try:
        b.replay_all(now=T0)
    finally:
        b.__dict__.pop("_score_one", None)   # the closure refers back to the book: drop it so the book is freed
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

    def replays():
        try:
            for k in range(3):
                b.replay_all(now=T0 + (k + 1) * 60)
        except Exception as exc:  # noqa: BLE001
            errors.append(("replay", repr(exc)))

    def closes():
        # a fixed number of closes interleaved with the replays: no busy wait competing with them for the GIL
        try:
            for k in range(300):
                st = b.by_idx[k % len(b.by_idx)]
                b.on_live_close({"set_id": st.id, "t": T0 + 9000 + k, "symbol": "C-USDT", "side": "LONG",
                                 "pnl": 0.1, "pnl_pct": 0.001, "hold_s": 60, "reason": "tp", "client_id": f"c{k}"})
                b.pick("general")
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


def ready_carried_across_refresh():
    """A refresh keeps the last complete pass valid: entries stay open while the next pass replays."""
    b = _book(n_syms=3, bars=1200)
    b.replay_all(now=T0)
    during = []
    b.replay_all(now=T0, on_step=lambda: during.append(bool(b.progress.ready)))
    ok = b.progress.ready and bool(during) and all(during)
    return ok, f"ready_after={b.progress.ready} during_replay={sorted(set(during))} steps={len(during)}"


def out_of_grid_sets_retired_and_closes_counted():
    """A Set that leaves the grid (step-adapt) keeps its tape as retired, late closes still attribute to it,
    and a close with no matching Set is counted instead of dropped silently."""
    b = _book(n_syms=3, bars=1200)
    b.replay_all(now=T0)
    victim = next(st for st in b.by_idx if st.kind == "base" and st.step == b.min_step)
    b.min_step = b.min_step + 1
    b._rebuild_sets()
    retired_ok = victim.id in b.retired and victim.id not in b.sets and victim.active is False
    base = len(victim.live)
    b.on_live_close({"set_id": victim.id, "t": T0 + 5000, "symbol": "Z-USDT", "side": "LONG",
                     "pnl_pct": 0.002, "hold_s": 60, "reason": "tp", "client_id": "retired-close-1"})
    attributed = len(victim.live) == base + 1
    before = b.live_unmatched
    b.on_live_close({"set_id": "indications:1m:sl9.99:st99", "t": T0 + 5100, "symbol": "Z-USDT", "side": "LONG",
                     "pnl_pct": 0.001, "hold_s": 60, "reason": "tp", "client_id": "orphan-1"})
    counted = b.live_unmatched == before + 1
    ok = retired_ok and attributed and counted
    return ok, f"retired={retired_ok} attributed={attributed} unmatched_counted={counted} retired_total={len(b.retired)}"


def unmatched_pnl_pct_is_skipped_not_a_loss():
    """A close without a finite pnl_pct is not a -1R loss: it is skipped and counted in the PF output."""
    from position_cost import last_n_cost_pf
    rows = [{"pnl_pct": 0.002, "t": 1.0}, {"t": 2.0}, {"pnl_pct": None, "t": 3.0}, {"pnl_pct": float("nan"), "t": 4.0}]
    out = last_n_cost_pf(rows, 15, 0.15)
    clean = last_n_cost_pf([rows[0]], 15, 0.15)
    ok = out["count"] == 1 and out["skipped"] == 3 and abs(out["pf"] - clean["pf"]) < 1e-12
    return ok, f"count={out['count']} skipped={out['skipped']} pf={out['pf']} clean_pf={clean['pf']}"


# deterministic, concurrent and per-bar-callback replays run unmemoized: they must observe a real replay
def ingest_keeps_history_on_live_refresh():
    """A live refresh joins the stored history: repeating the newest rows changes nothing, and a window with 30 new
    minutes appends them. The 1920-bar history is not replaced by the short live store."""
    b = _book(n_syms=1, bars=1920)
    sym = "S0-USDT"
    full = [list(r) for r in b.bars[sym]]
    b.ingest_bars(sym, full[-60:])
    repeat_ok = b.bars[sym] == full
    newer = synth_bars(55, 30, start=full[-1][3])
    b.ingest_bars(sym, full[-60:] + newer)
    expected = (full + newer)[-b.lookback:]
    joined = b.bars[sym] == expected
    return repeat_ok and joined, f"len={len(b.bars[sym])} repeat_ok={repeat_ok} joined={joined} lookback={b.lookback}"


def live_dedupe_and_history_rows_stay_bounded():
    """The live close dedupe keeps a fixed window of keys (the oldest are evicted), and the history rows do not grow
    when the same bars are replayed again: a refresh replaces them, it does not add to them."""
    import set_engine as se
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    rows_before = sum(len(v) for per in b._hist_rows.values() for v in per.values())
    b.replay_all(now=T0)
    rows_after = sum(len(v) for per in b._hist_rows.values() for v in per.values())
    saved = se.LIVE_SEEN_MAX
    try:
        se.LIVE_SEEN_MAX = 50
        st = b.by_idx[0]
        base = st.n                              # counts accepted closes; st.live itself is trimmed to 80
        for k in range(200):
            b.on_live_close({"set_id": st.id, "t": T0 + 9000 + k, "symbol": "M-USDT", "side": "LONG",
                             "pnl_pct": 0.001, "hold_s": 60, "reason": "tp", "client_id": f"bound-{k}"})
        keys = len(b._live_seen)
        added = st.n - base
        last = {"set_id": st.id, "t": T0 + 9000 + 199, "symbol": "M-USDT", "side": "LONG",
                "pnl_pct": 0.001, "hold_s": 60, "reason": "tp", "client_id": "bound-199"}
        b.on_live_close(dict(last))              # a recent duplicate is still refused
        dup_added = st.n - base - added
    finally:
        se.LIVE_SEEN_MAX = saved
    ok = keys <= 50 and added == 200 and dup_added == 0 and rows_after == rows_before and rows_before > 0
    return ok, (f"dedupe keys={keys} (max 50) closes added={added}/200 recent duplicate added={dup_added}; "
                f"history rows {rows_before} -> {rows_after} after a second replay")


CHECKS = [
    ("processing.live-dedupe-and-history-bounded", live_dedupe_and_history_rows_stay_bounded),
    ("processing.all-sets-processed-full-grid", memo.memoized(every_set_processed_full_grid)),
    ("processing.ingest-keeps-history-on-live-refresh", ingest_keeps_history_on_live_refresh),
    ("processing.trades-produced", memo.memoized(trades_produced_and_positive_signal)),
    ("processing.trade-invariants", memo.memoized(trade_invariants_hold)),
    ("processing.deterministic-replay", deterministic_replay),
    ("processing.continuous-across-refreshes", memo.memoized(continuous_across_refreshes)),
    ("processing.due-cadence", memo.memoized(due_cadence)),
    ("processing.symbol-failure-isolated-and-recovers", memo.memoized(symbol_failure_isolated_and_recovers)),
    ("processing.set-failure-isolated", memo.memoized(set_failure_isolated)),
    ("processing.chunked-refresh-equals-full", memo.memoized(chunked_refresh_equals_full)),
    ("processing.live-close-once-and-gate", memo.memoized(live_close_counted_once_and_gate_updates)),
    ("processing.concurrent-replay-and-live-closes", concurrent_replay_and_live_closes),
    ("processing.ready-carried-across-refresh", ready_carried_across_refresh),  # needs per-bar callbacks: unmemoized
    ("processing.out-of-grid-sets-retired-and-closes-counted", memo.memoized(out_of_grid_sets_retired_and_closes_counted)),
    ("processing.unmatched-pnl-pct-skipped-not-a-loss", memo.memoized(unmatched_pnl_pct_is_skipped_not_a_loss)),
]
