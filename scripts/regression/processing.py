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
    ok = p.phase == "ready" and p.ready and p.sets_done == len(b.by_idx) == 1538 and not unscored and p.errors == 0
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


def live_close_stays_counted_through_a_replay():
    """A replay pass recounts the history it replays. A live close accepted before the pass must still be in the
    Set's fill count afterwards: n is replayed fills plus live closes."""
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    st = b.by_idx[0]
    before = st.n
    b.on_live_close({"set_id": st.id, "t": T0 + 5000, "symbol": "L-USDT", "side": "LONG", "pnl": 0.25,
                     "pnl_pct": 0.0025, "hold_s": 60, "reason": "tp", "client_id": "cid-count-1"})
    after_live = st.n
    b.replay_all(now=T0)
    after_replay = st.n
    ok = after_live == before + 1 and after_replay == before + 1 and st.live_n == 1
    return ok, f"n before={before} after live close={after_live} after replay={after_replay} live_n={st.live_n}"


def rebuild_mid_pass_scores_no_partial_tape():
    """The Set map is rebuilt between two symbols of one replay pass. The pass must not score a Set that appeared
    during the pass on a tape missing the earlier symbols, and surviving Sets keep their last complete scores."""
    b = _book(n_syms=3, bars=1200)
    full_max = b.step_max
    b.step_max = full_max - 3              # a smaller grid first, so the mid-pass rebuild adds Sets
    b._rebuild_sets()
    b.replay_all(now=T0)
    before = {st.id: st.n for st in b.by_idx}
    new_grid_ids = set()
    orig = b._replay_symbol
    calls = {"n": 0}

    def hook(symbol, hist, now, on_step=None):
        orig(symbol, hist, now, on_step=on_step)
        calls["n"] += 1
        if calls["n"] == 1:
            b.step_max = full_max
            b._rebuild_sets()
            new_grid_ids.update(sid for sid in b.sets if sid not in before)

    b._replay_symbol = hook
    try:
        b.replay_all(now=T0)
    finally:
        b._replay_symbol = orig
    new_unscored = all(b.sets[sid].n == 0 and b.sets[sid].gate_n == 0 for sid in new_grid_ids)
    kept = all(b.sets[sid].n == before[sid] for sid in before if sid in b.sets)
    ok = bool(new_grid_ids) and new_unscored and kept and b.progress.phase == "partial"
    return ok, (f"new Sets={len(new_grid_ids)} unscored={new_unscored} survivors kept={kept} "
                f"phase={b.progress.phase}")

def snapshot_waits_for_a_writer_mid_update():
    """A live close holds the Set lock while it scores. While the writer is held inside gate_reason, no other thread
    may get into the gate, because a reader that got in would see the writer's half-updated Set. Observed without
    timing: the reader either waits on the lock (never reaches the gate while held) or it reaches the gate and is
    reported. After release the snapshot must complete."""
    import threading
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    st = b.by_idx[0]
    orig = b.gate_reason
    entered = threading.Event()
    release = threading.Event()
    reader_inside = threading.Event()

    def gated(s):
        r = orig(s)
        if threading.current_thread().name == "writer":
            if s is st:
                entered.set()
                release.wait(10)
        elif entered.is_set() and not release.is_set():
            reader_inside.set()
            release.wait(10)
        return r

    b.gate_reason = gated
    out = {}
    rec = {"set_id": st.id, "t": T0 + 7000, "symbol": "H-USDT", "side": "LONG", "pnl": 0.1, "pnl_pct": 0.001,
           "hold_s": 60, "reason": "tp", "client_id": "cid-handshake-1"}
    w = threading.Thread(target=b.on_live_close, args=(rec,), name="writer")
    w.start()
    try:
        if not entered.wait(10):
            return False, "the writer never reached the gate"
        r = threading.Thread(target=lambda: out.setdefault("snap", b.snapshot()), name="reader")
        r.start()
        got_in = reader_inside.wait(2.0)     # a reader that is not blocked by the lock gets into the gate now
        release.set()
        w.join(10)
        r.join(10)
    finally:
        b.gate_reason = orig
        release.set()
    finished = not r.is_alive() and "snap" in out
    ok = (not got_in) and finished
    return ok, f"reader got into the gate while the writer held the Set={got_in}; snapshot finished after release={finished}"

def departed_symbols_leave_every_cache():
    """A symbol that leaves the universe drops its replay rows and its signal memo at the next pass."""
    b = _book(n_syms=3, bars=600)
    b.replay_all(now=T0)
    gone = sorted(b.bars)[0]
    had_rows = gone in b._hist_rows
    del b.bars[gone]
    b.replay_all(now=T0)
    ok = had_rows and gone not in b._hist_rows and gone not in b._sig_cache and len(b._sig_cache) > 0
    return ok, f"{gone}: rows dropped={gone not in b._hist_rows} memo dropped={gone not in b._sig_cache} memo keys left={len(b._sig_cache)}"


def pick_any_has_no_gap_between_lookups():
    """pick_any decides from one lock acquisition. A probe wraps the public pick(): if the base lookup returns through
    pick() (the two-call path), the lock is free before the trail lookup, and a thread that enters the book in that
    gap can change the decision. The check fails when such a gap exists."""
    import threading
    from set_engine import SetBook
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    pack = b.by_idx[0].pack
    seen = {"gap": False, "entered": False}
    orig_pick = SetBook.pick

    def probed(p, kind="base"):
        r = orig_pick(b, p, kind)
        if kind == "base" and not seen["gap"]:
            seen["gap"] = True
            done = threading.Event()
            t = threading.Thread(target=lambda: (b.counts(), done.set()), name="gap-probe")
            t.start()
            seen["entered"] = done.wait(2.0)
            t.join(5)
        return r

    b.pick = probed
    try:
        b.pick_any(pack)
    finally:
        del b.pick
    ok = not (seen["gap"] and seen["entered"])
    return ok, f"gap between base and trail lookups={seen['gap']} a thread entered it={seen['entered']}"


def effective_pf_is_pooled_over_valid_sets_only():
    """The snapshot reports the effective PF (Sets that pass Base validation) apart from the unfiltered PF of every Set.
    The base figure is rebuilt here from the valid Sets' own gross profit and loss, so a mix-up of the two pools fails."""
    b = _book(n_syms=3, bars=900, mainGateEnabled=False, stagesEnabled=False, **SMALL)     # the pools of the Base gate (the floor is moved below)
    b.replay_all(now=T0)
    pfs = sorted(st.gate_pf for st in b.by_idx if st.gate_n >= b.gate_min)
    b.min_pf = pfs[len(pfs) // 2] if pfs else 0.0   # the median: some Sets pass Base, some do not
    snap = b.snapshot()["pfBase"]
    valid = [st for st in b.by_idx if not b.gate_reason(st)]
    def pf(sts):
        gp = sum(x.gate_gp for x in sts); gl = sum(x.gate_gl for x in sts)
        return round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)
    ok = (snap["base"]["sets"] == len(valid) and snap["unfiltered"]["sets"] == len(b.by_idx)
          and snap["base"]["pf"] == pf(valid) and snap["unfiltered"]["pf"] == pf(b.by_idx)
          and 0 < snap["base"]["sets"] < snap["unfiltered"]["sets"])
    want_pct = round(100.0 * len(valid) / len(b.by_idx), 2)
    packs = snap["byPack"]
    ok = ok and snap["validatedPct"] == want_pct and snap["valid"] == len(valid) and snap["total"] == len(b.by_idx) \
        and sum(v["valid"] for v in packs.values()) == len(valid) and sum(v["total"] for v in packs.values()) == len(b.by_idx) \
        and set(packs) == set(b.packs)
    return ok, (f"base pf={snap['base']['pf']} over {snap['base']['sets']} Sets; unfiltered pf={snap['unfiltered']['pf']} "
                f"over {snap['unfiltered']['sets']}; validated {snap['validatedPct']}% ({snap['valid']}/{snap['total']}) packs={list(packs)}")


def stage_figures_average_the_valid_sets_and_report_the_validated_share():
    """pfBase.stages gives, per stage (Base, Main, Real, Real Negative, every valid Set), how many Sets pass it, their share of ALL configured Sets,
    the pooled PF of those Sets' own window orders and the mean of their own PFs. Rebuilt here from each Set's own gross profit and loss: a figure
    taken over all Sets (the overall average) or over the wrong stage's Sets fails."""
    b = _book(n_syms=3, bars=900, **SMALL)
    b.replay_all(now=T0)
    b.min_pf = 0.3                                   # a low floor so that Sets pass each stage on this synthetic tape
    for st in b.by_idx:
        b._score_one(st)
    snap = b.snapshot()["pfBase"]
    stg = snap["stages"]
    from set_engine import STAGE_REASONS, RANK_PF_CAP, pf_of
    reasons = {x.id: b.gate_reason(x) for x in b.by_idx}
    base_only = [x for x in b.by_idx if reasons[x.id] in ("",) + STAGE_REASONS]
    main_pass = [x for x in base_only if (x.stage or {}).get("main") and (x.stage or {}).get("auto")]
    valid = [x for x in b.by_idx if not reasons[x.id]]
    pos = [x for x in valid if b.lane_of(x) == "pos"]
    neg = [x for x in valid if b.lane_of(x) == "neg"]
    total = len(b.by_idx)

    def want(sts, gp, gl):
        gps, gls = sum(getattr(x, gp) for x in sts), sum(getattr(x, gl) for x in sts)
        pfs = [min(RANK_PF_CAP, pf_of(getattr(x, gp), getattr(x, gl))) for x in sts]
        return len(sts), round(100.0 * len(sts) / total, 2), round(pf_of(gps, gls), 4), round(sum(pfs) / len(pfs), 4) if pfs else 0.0
    rows = {"base": (base_only, "gate_gp", "gate_gl"), "main": (main_pass, "main_gp", "main_gl"), "real": (pos, "exe_gp", "exe_gl"),
            "realNeg": (neg, "neg_gp", "neg_gl"), "valid": (valid, "gate_gp", "gate_gl")}
    bad = {k: (got, w) for k, (sts, gp, gl) in rows.items()
           for got, w in [((stg[k]["sets"], stg[k]["pct"], stg[k]["pf"], stg[k]["meanPf"]), want(sts, gp, gl))] if got != w}
    allsets = pf_of(sum(x.gate_gp for x in b.by_idx), sum(x.gate_gl for x in b.by_idx))
    ok = (not bad and stg["allSets"] == total and 0 < stg["valid"]["sets"] <= stg["real"]["sets"] + stg["realNeg"]["sets"] <= stg["main"]["sets"] + stg["realNeg"]["sets"]
          and 0 < stg["main"]["sets"] <= stg["base"]["sets"] < total
          and stg["valid"]["pf"] == snap["base"]["pf"] and abs(stg["base"]["pf"] - round(allsets, 4)) > 1e-9)
    return ok, (f"sets / % of {total} / pooled PF / mean PF per stage: " + "; ".join(f"{k} {stg[k]['sets']} / {stg[k]['pct']}% / {stg[k]['pf']} / {stg[k]['meanPf']}" for k in rows)
                + f"; all-Sets PF {allsets:.3f}; mismatches {bad}")


def sim_gate_equals_the_engine_for_every_window_configuration():
    """The walk-forward simulator's direct gate (stage_eval.gate_metrics) and the engine (SetBook._score_one / gate_reason / lane_of / rank_key) give the same
    verdict, lane, window figures and rank for sampled (Set, hour) pairs under every window configuration: the Base long / short / minimal windows (1-3 results,
    positive and negative), Main static and Auto, Real (down to 1), the Negative windows (1,2 and 5,10), both window rules and the stages off. Each configuration
    is loaded into its own SetBook: no state is shared between them."""
    import json as _json
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", ""))
    import stage_eval as sg
    from common import SMALL, T0, synth_bars
    names = ["S0-USDT", "S1-USDT"]
    bars = {s_: synth_bars(21 + i, 1100, drift=0.0006 if i % 2 == 0 else -0.0006, vol=0.003) for i, s_ in enumerate(names)}
    times = {s_: [int(T0) + 60 * k for k in range(len(bars[s_]))] for s_ in names}
    base_patch = {**SMALL, "holdEnabled": False, "histLookbackBars": 500, "histLookbackAuto": False, "stratGeneral": True, "stratIndications": True,
                  "setGateWindow": 20, "setGateShortN": 8, "mainGateN": 8, "mainAutoWindows": [4], "realGateN": 4, "setMaxDdtHours": 36, "setGateMinTrades": 0}
    saved = _os.environ.get("STAGE_OVERLAY_PATCH")
    try:
        _os.environ["STAGE_OVERLAY_PATCH"] = _json.dumps(base_patch)
        shadow = {}
        for s_ in names:
            _, warm, dirs, confs, out = sg._symbol_job((s_, bars[s_], times[s_][0]))
            shadow[s_] = (warm, dirs, confs, out)
        configs = {
            "shipped stages": {},
            "minimal +1": {"setGateMinN": 1}, "minimal +2": {"setGateMinN": 2}, "minimal +3": {"setGateMinN": 3},
            "minimal -1": {"setGateMinN": 1, "setGateMinMode": "negative"}, "minimal -2": {"setGateMinN": 2, "setGateMinMode": "negative"},
            "minimal -3": {"setGateMinN": 3, "setGateMinMode": "negative"},
            "minimal -2 full windows": {"setGateMinN": 2, "setGateMinMode": "negative", "stageFullWindow": True},
            "Real 1, Negative 1,2": {"realGateN": 1, "mainNegWindows": "1,2"}, "Real 5, Negative 1,2, minimal +1": {"realGateN": 5, "mainNegWindows": "1,2", "setGateMinN": 1},
            "full windows": {"stageFullWindow": True}, "no short window": {"setGateShortN": 0, "setGateMinN": 2},
            "Main static 12, Auto 5": {"mainGateN": 12, "mainAutoWindows": [5, 6]}, "long 30, short 12": {"setGateWindow": 30, "setGateShortN": 12},
            "stages off": {"stagesEnabled": False},
        }
        res = {}
        for label, extra in configs.items():
            _os.environ["STAGE_OVERLAY_PATCH"] = _json.dumps({**base_patch, **extra})
            book = sg.make_book()
            book.min_pf = 0.6                                   # a low floor so that Sets pass each window on this synthetic tape
            tapes = sg.Tapes(book, names, times, shadow)
            start = max(times[s_][0] for s_ in names) + book.lookback * 60
            end = min(times[s_][-1] for s_ in names)
            hours = [float(h) for h in range(int(start // 3600 + 1) * 3600, int(end), 3600)]
            n_chk, bad = sg.self_check(book, tapes, hours, samples=36)
            valid_share = sum(1 for st in book.by_idx for h in hours[::3] if sg.gate_metrics(book, tapes, st, h)[0]) / max(1, len(book.by_idx) * len(hours[::3]))
            res[label] = (n_chk, bad, round(100 * valid_share, 1))
    finally:
        if saved is None:
            _os.environ.pop("STAGE_OVERLAY_PATCH", None)
        else:
            _os.environ["STAGE_OVERLAY_PATCH"] = saved
    bad_all = {k: v for k, v in res.items() if v[1] or v[0] < 20}
    return not bad_all, f"{len(res)} configurations, {sum(v[0] for v in res.values())} sampled pairs, mismatches {bad_all or 0}; valid share % " + ", ".join(f"{k}: {v[2]}" for k, v in res.items())


def the_signal_memo_holds_one_entry_per_bar_for_every_pack():
    """pack_signals memoises each bar's signals under that bar's window key. An extra pack's loop once reused the memo key name
    and stored every bar under one entry: no hit was ever possible. One entry per scored bar, and a second pass reads them."""
    import set_engine as se
    b = SetBook()
    b.load(overlay(stratMulti=True, stratTrend=True, **SMALL))
    bars = synth_bars(9, 420)
    cache = {}
    first = se.pack_signals(bars, b.packs, b.ind_settings, 0.0, 100, cache=cache, salt=7)
    n = len(cache)
    keys = set(cache)
    second = se.pack_signals(bars, b.packs, b.ind_settings, 0.0, 100, cache=cache, salt=7)
    ok = n == len(bars) - 100 and set(cache) == keys and first == second and {"multi", "trend", "indications"} <= set(b.packs)
    return ok, f"memo entries={n} (scored bars {len(bars) - 100}); same keys on the second pass={set(cache) == keys}; identical signals={first == second}"


def pack_change_does_not_poison_the_signal_memo():
    """The signal memo is kept across a reload. A reload that switches a pack on must not find memo entries written
    without that pack: the replay has to complete, not fail with a missing pack on every symbol."""
    b = _book(n_syms=2, bars=600, stratGeneral=False, **SMALL)
    b.replay_all(now=T0)
    b.load(overlay(stratGeneral=True, **SMALL))
    b.replay_all(now=T0)
    errs = [st.last_error for st in b.by_idx if st.last_error]
    ok = b.progress.phase != "error" and not errs and "general" in b.packs
    return ok, f"phase={b.progress.phase} packs={b.packs} errors={errs[:1]}"


def effective_pf_uses_the_rolling_window_not_the_whole_tape():
    """A Set that earned its profit long ago and loses now must not carry the pool: the pooled PF is taken over each
    Set's last gate-window orders only."""
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    st = b.by_idx[0]
    good = [{"t": 1000.0 + i, "pnl": 0.01, "pnl_pct": 0.0112, "symbol": "X", "side": "LONG", "hold_s": 60, "reason": "tp", "exit_i": i}
            for i in range(40)]
    bad = [{"t": 2000.0 + i, "pnl": -0.003, "pnl_pct": 0.0, "symbol": "X", "side": "LONG", "hold_s": 60, "reason": "sl", "exit_i": 100 + i}
           for i in range(10)]
    b.gate_window = 10
    st.hist = good + bad
    st.live = []
    b._score_one(st)
    # whole tape: 40 wins of +1% against 10 losses of -0.12%: PF far above 1; last 10 orders: all losses
    ok = st.gate_pf == 0.0 and st.gate_gp == 0.0 and st.gate_gl > 0 and st.gp > 0 and st.n >= 0
    return ok, f"gate pf={st.gate_pf} gate gp={st.gate_gp} gl={st.gate_gl}; whole tape gp={st.gp:.4f}"


def short_range_has_its_own_pf_floor():
    from set_engine import SHORT_PACK
    b = SetBook()
    b.load(overlay(stratShortRange=True, setMinPf=2.0, mainGateEnabled=False, stagesEnabled=False, **SMALL))
    sr = next(s for s in b.by_idx if s.pack == SHORT_PACK and s.kind == "base")
    other = next(s for s in b.by_idx if s.pack != SHORT_PACK and s.kind == "base")
    for st in (sr, other):
        st.gate_n = b.gate_min
        st.gate_pf = 1.3          # above Short Range's 1.25, below the global 2.0
        st.max_dd_s = 0.0
        st.active = True
        st.last_error = ""
        st.locked = False
    b.load(overlay(stratShortRange=True, setMinPf=2.0, shortRangeMinPf=1.5, mainGateEnabled=False, stagesEnabled=False, **SMALL))
    reason_default = None
    b2 = SetBook()
    b2.load(overlay(stratShortRange=True, setMinPf=2.0, mainGateEnabled=False, stagesEnabled=False, **SMALL))
    ok = (b.pack_min_pf[SHORT_PACK] == 1.5 and b2.pack_min_pf[SHORT_PACK] == 1.25
          and b2.gate_reason(sr) == "" and b2.gate_reason(other) == "pf<min")
    return ok, f"short range floor default={b2.pack_min_pf[SHORT_PACK]} set={b.pack_min_pf[SHORT_PACK]}; PF 1.2: short range='{b2.gate_reason(sr)}' other='{b2.gate_reason(other)}'"


def pf_progress_history_is_continuous_and_bounded():
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    h1 = list(b.pf_history)
    b.pf_history[-1]["t"] -= 30.0           # a later pass, outside the 20 s throttle
    b._record_pf_progress()
    b._record_pf_progress()                 # inside the throttle: no second point
    h2 = list(b.pf_history)
    for i in range(800):
        b.pf_history.append({"t": 1.0 + i, "pf": 1.0, "n": 1, "valid": 1, "validatedPct": 1.0, "unfiltered": 1.0})
    b._record_pf_progress()
    snap = b.snapshot()["pfBase"]
    ok = len(h1) == 1 and len(h2) == 2 and len(b.pf_history) <= 720 and len(snap["history"]) <= 240 and snap["basis"] == "gate-window"
    return ok, f"points after one pass={len(h1)} after two={len(h2)} bounded={len(b.pf_history)} snapshot={len(snap['history'])}"


def legacy_pick_prefers_judged_sets_over_a_single_lucky_order():
    b = SetBook()
    b.load(overlay(setUseHistoricGate=False, mainGateEnabled=False, stagesEnabled=False, **SMALL))
    base = [s for s in b.by_idx if s.kind == "base" and s.pack == b.packs[0]]
    lucky, solid = base[0], base[1]
    for st, n, pf in ((lucky, 1, 99.0), (solid, 40, 4.0)):
        st.gate_n, st.gate_pf, st.n, st.active, st.last_error, st.locked = n, pf, n, True, "", False   # with the minimum off the rank shrinks a PF by its orders
    for st in base[2:]:
        st.gate_n, st.gate_pf = 0, 0.0
    got = b.pick(b.packs[0])
    return got is solid, f"picked {got and got.id} (n={got and got.gate_n}, pf={got and got.gate_pf}) over the one-order Set"


def the_base_gate_cannot_be_switched_off():
    """Base evaluation gates are always enabled: an overlay that says otherwise is ignored."""
    b = SetBook()
    b.load(overlay(setUseHistoricGate=False, mainGateEnabled=False, stagesEnabled=False, **SMALL))
    on = b.use_historic_gate is True and b.snapshot()["useHistoricGate"] is True
    return on, f"overlay setUseHistoricGate=false -> engine gate on={on}"


def step_adapt_waits_for_the_pass_in_flight():
    b = _book(n_syms=2, bars=600, **SMALL)
    b.replay_all(now=T0)
    losing = [{"pnl": 0.001, "pnl_pct": 0.003, "t": float(i)} for i in range(4)] + \
             [{"pnl": -0.01, "pnl_pct": 0.0001, "t": float(10 + i)} for i in range(16)]   # a losing average with 4 good fills
    gen0, step0 = b._topo_gen, b.min_step
    b._running = True                                   # a pass is in flight
    b.adapt_from_live(losing)
    unchanged = b._topo_gen == gen0 and b.min_step == step0 and b._adapt_pending is not None
    b._running = False
    b._after_pass()
    applied = b._topo_gen == gen0 + 1 and b._adapt_pending is None
    return unchanged and applied, f"during the pass: topology {gen0}->{gen0} min_step {step0}; after it: topology {b._topo_gen} min_step {b.min_step}"


def short_range_trailing_stays_inside_its_steps():
    from set_engine import SHORT_PACK
    b = SetBook()
    b.load(overlay(stratShortRange=True, exitIgnoreTp=True))
    arms = sorted({s.trail_arm for s in b.by_idx if s.pack == SHORT_PACK and s.kind == "trail"})
    others = sorted({s.trail_arm for s in b.by_idx if s.pack != SHORT_PACK and s.kind == "trail"})
    cap = max(b.short_steps) * b.cost_pct
    return bool(arms) and max(arms) <= cap + 1e-9 and max(others) > cap, f"short range arms {arms[0]}..{arms[-1]} cap {cap}; other packs reach {others[-1]}"


def a_set_must_pass_on_both_halves_of_its_window_when_asked():
    """A Set whose window PF clears the floor only because the newer half is excellent is "unstable" when the
    consistency floor is on, and valid when it is off (the default)."""
    def tape(first, second):
        rows = []
        for i, g in enumerate(first + second):
            rows.append({"t": 1000.0 + i, "pnl": g - 0.0012, "pnl_pct": g, "symbol": "X", "side": "LONG", "hold_s": 60,
                         "reason": "tp" if g > 0.0012 else "sl", "exit_i": i})
        return rows
    first = [0.0000] * 20 + [0.0040] * 5          # older half: PF well under 1
    second = [0.0100] * 22 + [0.0000] * 3          # newer half: excellent
    out = {}
    for cons in (0.0, 1.0):
        b = SetBook()
        b.load(overlay(setGateWindow=50, setGateMinTrades=30, setMinPf=1.1, setGateConsistencyPf=cons, mainGateEnabled=False, stagesEnabled=False, **SMALL))
        st = next(x for x in b.by_idx if x.kind == "base")
        st.hist = tape(first, second)
        st.live = []
        b._score_one(st)
        out[cons] = (b.gate_reason(st), round(st.gate_pf, 2), st.gate_pf_old, st.gate_pf_new)
    ok = out[0.0][0] == "" and out[1.0][0] == "unstable" and out[1.0][2] < 1.0 < out[1.0][3]
    return ok, f"off={out[0.0]} on={out[1.0]} (reason, window PF, old half PF, new half PF)"


def base_lane_lookup_equals_the_walk():
    """The indexed exit search (base lanes, and trailing lanes until they arm) must give exactly the walk's trades, open position and cooldown, in one
    range and in chunks, with TP honoured and ignored, both directions, on several seeds."""
    import random
    from set_engine import BarIndex, SetLane
    diffs = 0
    checked = 0
    for seed in (3, 7, 11):
        rnd = random.Random(seed)
        bars = synth_bars(seed, 900, drift=0.0003 if seed % 2 else -0.0003)
        acc = BarIndex(bars)
        dirs = [rnd.choice((0, 0, 0, 1, -1)) for _ in bars]
        confs = [0.9 if d else 0.0 for d in dirs]
        cand = [i for i, d in enumerate(dirs) if d]
        for honor in (True, False):
            b = SetBook()
            b.load(overlay(exitIgnoreTp=not honor, **SMALL))
            cfg = {"entry_conf": 0.58, "time_bars": 45, "scratch_bars": 10, "scratch_min": 0.0016, "cooldown": 2,
                   "cost_pct": COST, "honor_tp": honor}
            for st in list(b.by_idx):
                for chunks in ((0, 900), (0, 300, 301, 650, 900)):
                    l1, l2 = SetLane(st, "X"), SetLane(st, "X")
                    a1, a2 = [], []
                    for c0, c1 in zip(chunks[:-1], chunks[1:]):
                        a1 += l1.scan(c0, c1, bars, dirs, confs, True, cfg, cand)
                        a2 += l2.scan(c0, c1, bars, dirs, confs, True, cfg, cand, acc)
                    checked += 1
                    if a1 != a2 or l1.open != l2.open or l1.cool != l2.cool:
                        diffs += 1
    return diffs == 0 and checked > 0, f"lanes compared={checked} differing={diffs}"


def scratch_lookup_keeps_the_exact_level():
    """The scratch lookup narrows with a level 1e-9 looser than the exact one. A close inside that gap (just under the
    exact scratch level) must not scratch: the lookup gives the walk's result, here no exit until the time bar."""
    from set_engine import BarIndex, SetLane
    b = SetBook()
    b.load(overlay(**SMALL))
    st = b.by_idx[0]
    cfg = {"entry_conf": 0.58, "time_bars": 45, "scratch_bars": 10, "scratch_min": 0.0016, "cooldown": 2,
           "cost_pct": COST, "honor_tp": True}
    near = 100.0 * (1 + 0.0016) * (1 - 3e-10)      # under the exact level, over the loose one
    bars = [[100.0, 100.01, 99.99, 100.0, 1.0] for _ in range(16)] + [[near, near + 0.01, near - 0.01, near, 1.0]] * 64
    dirs = [0] * len(bars)
    dirs[5] = 1
    confs = [0.9 if d else 0.0 for d in dirs]
    l1, l2 = SetLane(st, "X"), SetLane(st, "X")
    a1 = l1.scan(0, len(bars), bars, dirs, confs, True, cfg, [5])
    a2 = l2.scan(0, len(bars), bars, dirs, confs, True, cfg, [5], BarIndex(bars))
    reasons = [t.get("reason") if isinstance(t, dict) else t for t in a2]
    return a1 == a2 and l1.open == l2.open, f"walk={a1} lookup={a2} reasons={reasons}"


def broken_or_flat_series_are_not_replayed():
    """A symbol with a broken print (a one-minute jump beyond histMaxJumpPct) or a mostly flat series (no trades in the
    minute) is left out of the replay and of entries, and it shows in the snapshot with its reason."""
    b = _book(n_syms=3, bars=600, **SMALL)
    spike = [list(r) for r in b.bars["S1-USDT"]]
    spike[300] = [spike[299][3], spike[299][3] * 3, spike[299][3], spike[299][3] * 3, 1.0]      # +200% in one minute
    b.bars["S1-USDT"] = spike
    flat = [list(r) for r in b.bars["S2-USDT"]]
    for k in range(0, 600, 2):
        c = flat[k][3]
        flat[k] = [c, c, c, c, 0.0]                  # half the minutes without a trade
    b.bars["S2-USDT"] = flat
    b.replay_all(now=T0)
    q = b.snapshot()["quality"]["symbols"]
    ok = (q.get("S1-USDT") == "spike" and q.get("S2-USDT") == "flat" and "S0-USDT" not in q
          and "S1-USDT" not in b._hist_rows and "S2-USDT" not in b._hist_rows and "S0-USDT" in b._hist_rows
          and not b.symbol_ok("S1-USDT") and b.symbol_ok("S0-USDT"))
    return ok, f"excluded={q} replayed={sorted(b._hist_rows)}"


def _rows(sid, wins, losses, t0=1000.0):
    """Rows in time order: losses first, wins last (so the newest orders are the wins) unless the caller reverses."""
    out, t = [], t0
    for _ in range(losses):
        t += 60
        out.append((t, "S0-USDT", "LONG", -0.0032, -0.002, 600.0, "sl", sid))
    for _ in range(wins):
        t += 60
        out.append((t, "S0-USDT", "LONG", 0.0028, 0.004, 600.0, "tp", sid))
    return out


def hold_auto_picks_each_sets_own_best_hold_from_its_last_orders():
    """Auto Mode: every Set (not its step, not its pack) keeps the hold whose own tape has the best cost PF over its last orders,
    at most holdAutoMax (50), at least holdAutoMin; the global time stop is the reference and a candidate must beat it by the
    margin. A manual step hold and the master switch override it."""
    from set_engine import HOLD_KEY
    b = _book(n_syms=1, bars=300)
    lo = next(x for x in b.by_idx if x.kind == "base" and x.step == 3)
    hi = next(x for x in b.by_idx if x.kind == "base" and x.step == 8)

    def run(st, base, cands):
        b._hist_rows = {"S0-USDT": {st.id: base, **{f"{st.id}{HOLD_KEY}{h}": r for h, r in cands.items()}}}
        rows = b._choose_hold(st, b._tape_rows(st.id, ["S0-USDT"], 0.0), ["S0-USDT"], 0.0)
        return st.hold_bars, st.hold_auto_n, len(rows)

    got = {}
    ref = _rows(lo.id, 20, 30)                                          # PF 0.9 at the global stop
    got["best"] = run(lo, ref, {10: _rows(lo.id, 10, 40), 20: _rows(lo.id, 45, 5), 30: _rows(lo.id, 25, 25)})
    # the last 50 orders decide: 40 old wins then 50 losses is a bad hold, 40 old losses then 50 wins a good one
    got["cap"] = run(lo, ref, {30: _rows(lo.id, 40, 0) + _rows(lo.id, 0, 50, t0=5000.0)})
    got["cap_good"] = run(lo, ref, {30: _rows(lo.id, 0, 40) + _rows(lo.id, 50, 0, t0=5000.0)})
    got["few"] = run(lo, ref, {15: _rows(lo.id, 5, 0)})                 # 5 orders, all wins: below holdAutoMin
    got["tie"] = run(lo, _rows(lo.id, 30, 20), {15: _rows(lo.id, 30, 20)})      # an equal PF keeps the global stop
    got["better"] = run(lo, _rows(lo.id, 30, 20), {15: _rows(lo.id, 31, 19)})   # +9%: replaces it
    # a PF only 1% above the global stop's is inside the margin and keeps the global stop; 5% above replaces it
    def with_loss(wins, losses, loss_pct):
        return [(1000.0 + 60 * k, "S0-USDT", "LONG", 0.0, (0.004 if k >= losses else loss_pct), 600.0, "x", lo.id) for k in range(wins + losses)]
    got["inside margin"] = run(lo, with_loss(30, 20, -0.002), {15: with_loss(30, 20, -0.00197)})
    got["outside margin"] = run(lo, with_loss(30, 20, -0.002), {15: with_loss(30, 20, -0.0018)})
    got["high"] = run(hi, _rows(hi.id, 10, 30), {120: _rows(hi.id, 40, 10), 10: _rows(hi.id, 50, 0)})   # 10 min is not a step-8 candidate
    man = _book(n_syms=1, bars=300, stepHoldOn=[False, False, True] + [False] * 9, stepHoldMin=[10, 10, 17] + [10] * 9)
    off = _book(n_syms=1, bars=300, holdEnabled=False, stepHoldOn=[True] * 12, stepHoldMin=[30] * 12)
    auto_off = _book(n_syms=1, bars=300, holdAuto=False)
    m3 = next(x for x in man.by_idx if x.kind == "base" and x.step == 3)
    m4 = next(x for x in man.by_idx if x.kind == "base" and x.step == 4)
    from set_engine import HOLD_AUTO_HIGH, HOLD_AUTO_LOW
    s5 = next(x for x in b.by_idx if x.kind == "base" and x.step == 5)
    s6 = next(x for x in b.by_idx if x.kind == "base" and x.step == 6)
    tr = next(x for x in b.by_idx if x.kind == "trail" and b.step_of(x) >= 6)
    ranges_ok = b.auto_holds(s5) == HOLD_AUTO_LOW and b.auto_holds(s6) == HOLD_AUTO_HIGH and b.auto_holds(tr) == HOLD_AUTO_HIGH
    ok = (ranges_ok and got["best"][0] == 20 and got["best"][1] == 50 and got["best"][2] == 50
          and got["cap"][0] == 0 and got["cap_good"][0] == 30 and got["cap_good"][1] == 50
          and got["few"][0] == 0 and got["tie"][0] == 0 and got["inside margin"][0] == 0 and got["outside margin"][0] == 15 and got["better"][0] == 15 and got["high"][0] == 120
          and lo.hold_auto and man.auto_holds(m3) == () and m3.hold_bars == 17 and not m3.hold_auto and man.auto_holds(m4) != ()
          and not off.auto_holds(off.by_idx[0]) and all(x.hold_bars == 0 for x in off.by_idx)
          and not auto_off.auto_holds(auto_off.by_idx[0]) and all(x.hold_bars == 0 for x in auto_off.by_idx))
    return ok, f"(hold, orders judged, tape rows) {got}; manual step 3 -> {m3.hold_bars} min, master off and auto off keep the global stop"


def hold_auto_replay_assigns_holds_and_judges_the_chosen_tape():
    """After a full pass each auto Set carries one of its candidate holds, its tape is the one of that hold (no time exit later
    than it), and the Sets stay deterministic across two passes."""
    from set_engine import HOLD_AUTO_HIGH, HOLD_AUTO_LOW
    b = _book(n_syms=3, bars=1200)
    b.replay_all(now=T0)
    first = {st.id: st.hold_bars for st in b.by_idx}
    bad, chosen = [], 0
    for st in b.by_idx:
        allowed = {0, *(HOLD_AUTO_LOW if b.step_of(st) <= 5 else HOLD_AUTO_HIGH)}
        if st.hold_bars not in allowed or not st.hold_auto:
            bad.append((st.id, st.hold_bars))
        if st.hold_bars:
            chosen += 1
            late = [r for r in st.hist if r["reason"] == "time" and r["hold_s"] > st.hold_bars * 60 + 1]
            if late:
                bad.append((st.id, "time exit after the chosen hold", late[0]["hold_s"]))
    b.replay_all(now=T0)
    same = first == {st.id: st.hold_bars for st in b.by_idx}
    return not bad and chosen > 0 and same, f"sets={len(first)} with an own hold={chosen} bad={bad[:3]} same_on_second_pass={same}"


def validation_windows_follow_the_overall_n_floor_8_on_the_gates_only():
    """pfWindow, setPfWindow, exitPfWindow, dcaPfWindow, slToTpRecalcN and trailRecalcN follow the overall validation's last N
    (setGateWindow) while windowsLinked is on (the default); unlinked each takes its own N, and no last-N validation reads fewer than
    8 orders (the hold Auto sample, the samples of Exit, Block and the coordinator, the Axis windows) whatever the setting. The Base gate's
    minimum order count (setGateMinTrades) is no part of that floor: 0 = off, any other value is taken as it is."""
    from block_engine import BlockBook
    from coord_engine import Coordinator
    from dca_engine import DcaBook
    from exit_engine import ExitBook
    from position_cost import MIN_VALIDATION_N, window_n
    from risk_variants import VariantBook
    import os, tempfile

    def windows(ov):
        ov = overlay(**ov)                      # the shipped overlay plus the case: every engine reads the same keys
        sb = SetBook(); sb.load(ov)
        co = Coordinator(); co.load({}, ov)
        ex = ExitBook(); ex.load(ov)
        vb = VariantBook(); vb.load(ov)
        return {"set": sb.pf_n, "coord": co.pf_window, "exit": ex.pf_n, "sl": vb.sl_recalc_n, "trail": vb.trail_recalc_n}

    own = {"pfWindow": 12, "setPfWindow": 12, "exitPfWindow": 12, "slToTpRecalcN": 12, "trailRecalcN": 12}
    tiny = {k: 3 for k in own}
    got = {
        "linked (shipped 50)": windows({}),
        "linked 30": windows({"setGateWindow": 30}),
        "linked ignores own values": windows({**own, "setGateWindow": 40}),
        "unlinked 12": windows({**own, "windowsLinked": False}),
        "unlinked 8": windows({**{k: 8 for k in own}, "windowsLinked": False}),
        "unlinked 3 -> floor": windows({**tiny, "windowsLinked": False}),
    }
    want = {
        "linked (shipped 50)": {k: 50 for k in got["linked (shipped 50)"]},
        "linked 30": {k: 30 for k in got["linked (shipped 50)"]},
        "linked ignores own values": {k: 40 for k in got["linked (shipped 50)"]},
        "unlinked 12": {k: 12 for k in got["linked (shipped 50)"]},
        "unlinked 8": {k: 8 for k in got["linked (shipped 50)"]},
        "unlinked 3 -> floor": {k: 8 for k in got["linked (shipped 50)"]},
    }
    sb = SetBook(); sb.load(overlay(setGateMinTrades=3, holdAutoMin=2))
    # only the validation gates and the five windows have the floor; the other counts stay as they were
    ex = ExitBook(); ex.load({"exitMinSamples": 2})
    co = Coordinator(); co.load({}, {"setMinSamples": 7, "prevPosMinCount": 1, "axisLastMaxWindow": 1})
    bb = BlockBook(os.path.join(tempfile.mkdtemp(prefix="ctsg-win-"), "b.json"), {"prevPosMinCount": 2})
    floors = {"hold_auto_min": sb.hold_auto_min}
    floors_ok = all(v >= MIN_VALIDATION_N for v in floors.values()) and sb.gate_min == 3     # the Base minimum order count has no floor (owner 2026-10-29)
    dc = DcaBook(); dc.load({"dcaPfWindow": 3, "windowsLinked": True})
    kept = {"dca_window": dc.pf_n, "exit_samples": ex.min_samples, "coord_samples": co.min_samples, "coord_prev_min": co.prev_min_count, "axis_last_window": co.axes["last"].max_window,
            "block_samples": bb.min_samples, "main_eval": co.main_eval, "real_eval": co.real_eval}
    kept_ok = kept == {"dca_window": 5, "exit_samples": 5, "coord_samples": 7, "coord_prev_min": 1, "axis_last_window": 1, "block_samples": 8, "main_eval": 20, "real_eval": 15}
    ok = got == want and floors_ok and kept_ok and window_n({}, "pfWindow") == 50
    return ok, f"windows {got}; floors {floors}; unchanged {kept}"


def _real_rows(sid, head_w, tail):
    """50 orders for the gate window: head_w winners first (+0.4%), then `tail` = [(pnl_pct, ...)] as the newest orders. Net of the 0.12% cost."""
    rows, t = [], 1000.0
    for _ in range(50 - len(tail)):
        t += 60
        rows.append((t, "S0-USDT", "LONG", 0.0028, 0.004, 600.0, "tp", sid))
    for x in tail:
        t += 60
        rows.append((t, "S0-USDT", "LONG", x - 0.0012, x, 600.0, "tp" if x > 0 else "sl", sid))
    return rows


MAIN_ONLY = {"setGateShortN": 0, "mainAutoEnabled": False, "mainNegEnabled": False, "realGateEnabled": False}   # the Main static gate alone


def real_gate_is_per_set_at_the_base_minimum_pf():
    """The Main gate: besides Base, a Set must hold the Base minimum PF over its own last mainGateN processed orders (default 25; the
    fixtures below read N 12). Per Set (another
    Set's tape never moves it), the same minimum PF as Base (setMinPf, the Short Range floor), reason "main", and the execution pool
    (valid_for / pick) holds only Sets that pass both. Switched off it is the Base gate alone."""
    def judged(main_on, tail, min_pf=1.25, **extra):
        extra.setdefault("mainGateN", 12)
        extra.setdefault("setGateMinTrades", 25)     # Base passes from order 35 (entries 10 minutes before their exit): 15 Main orders
        extra = {**MAIN_ONLY, **extra}
        b = _book(n_syms=1, bars=300, setMinPf=min_pf, mainGateEnabled=main_on, **extra)
        st = next(x for x in b.by_idx if x.kind == "base" and x.pack == b.packs[0])
        st.hist = [dict(zip(("t", "symbol", "side", "pnl", "pnl_pct", "hold_s", "reason", "set_id"), r)) for r in _real_rows(st.id, 38, tail)]
        st.live = []
        b._score_one(st)
        return b, st, b.gate_reason(st)
    good = [0.0062] * 6 + [-0.0028] * 6          # last 12: PF = 1.25 net of cost: exactly the minimum
    weak = [0.0061] * 6 + [-0.0028] * 6          # a hair under
    bad = [0.004] * 5 + [-0.002] * 7             # clearly under
    out = {}
    out["boundary 1.25"] = judged(True, good)[2]
    out["just under"] = judged(True, weak)[2]
    out["bad last 12"] = judged(True, bad)[2]
    out["bad, Real off"] = judged(False, bad)[2]
    out["min PF 1.5 moves Real too"] = judged(True, good, min_pf=1.5)[2]
    b8, st8, r8 = judged(True, bad, mainGateN=8)       # the last 8 orders of `bad`: 5 winners... a different N reads a different stretch
    out["N=8"] = (st8.main_n, r8)
    b_, st_, _ = judged(True, bad)
    # another Set never moves it: score a second Set with a wildly different tape, the first Set's Real numbers stay
    other = next(x for x in b_.by_idx if x is not st_ and x.kind == "base")
    before = (st_.main_pf, st_.main_n, b_.gate_reason(st_))
    other.hist = [dict(zip(("t", "symbol", "side", "pnl", "pnl_pct", "hold_s", "reason", "set_id"), r)) for r in _real_rows(other.id, 38, [0.01] * 12)]
    other.live = []
    b_._score_one(other)
    after = (st_.main_pf, st_.main_n, b_.gate_reason(st_))
    # the execution pool holds only Sets that pass both
    # fewer orders than N: Base is satisfied (window 40, minimum 8) but the Set has only 20 orders for a Real N of 30
    def short_tape(full):
        b20 = _book(n_syms=1, bars=300, mainGateEnabled=True, mainGateN=30, setGateWindow=40, setGateMinTrades=8, stageFullWindow=full, **MAIN_ONLY)
        s20 = next(x for x in b20.by_idx if x.kind == "base" and x.pack == b20.packs[0])
        s20.hist = [dict(zip(("t", "symbol", "side", "pnl", "pnl_pct", "hold_s", "reason", "set_id"), (1000.0 + 60 * k, "S0-USDT", "LONG", 0.0028, 0.004, 600.0, "tp", s20.id))) for k in range(20)]
        s20.live = []
        b20._score_one(s20)
        return s20.gate_n, s20.main_n, b20.gate_reason(s20)
    out["20 orders, Real N 30"] = short_tape(True)                       # the strict rule: the window must hold its 30 orders
    out["20 orders, Real N 30, available"] = short_tape(False)           # the default: judged on the 20 it has
    floor3 = _book(n_syms=1, bars=300, mainGateN=3)                        # an N under the floor is read as 8
    out["Real N 3"] = floor3.main_n
    b2, st2, _ = judged(True, bad)
    pool_off = [x.id for x in judged(False, bad)[0].valid_for(st2.pack, "base")]
    pool_on = [x.id for x in b2.valid_for(st2.pack, "base")]
    snap = judged(True, good)[0].snapshot()["pfBase"]
    ok = (out["boundary 1.25"] == "" and out["just under"] == "main" and out["bad last 12"] == "main" and out["bad, Real off"] == ""
          and out["min PF 1.5 moves Real too"] in ("pf<min", "main") and out["N=8"][0] == 8 and before == after
          and out["20 orders, Real N 30"][0] == 20 and out["20 orders, Real N 30"][1] < 30 and out["20 orders, Real N 30"][2] == "main"
          and out["20 orders, Real N 30, available"][2] == ""
          and out["Real N 3"] == 8
          and st2.id in pool_off and st2.id not in pool_on and "main" in snap and "real" in snap and "baseOnly" in snap)
    return ok, f"reasons {out}; other Set left it alone={before == after}; pool off/on holds the Set={st2.id in pool_off}/{st2.id in pool_on}; pfBase keys {sorted(snap)[:6]}"


def every_validation_gate_asks_the_base_minimum_pf():
    """Real, Exit, DCA, Block and the coordinator use the Base gate's minimum PF (setMinPf) while minPfLinked is on (the default); unlinked, Exit,
    DCA and the coordinator take their own value."""
    import os, tempfile
    from block_engine import BlockBook
    from coord_engine import Coordinator
    from dca_engine import DcaBook
    from exit_engine import ExitBook

    def gates(ov):
        sb = SetBook(); sb.load(overlay(**ov))
        co = Coordinator(); co.load({}, ov)
        ex = ExitBook(); ex.load(ov)
        dc = DcaBook(); dc.load(ov)
        pk = sb.packs[0]
        return {"main": sb.pack_min_pf.get(pk, sb.min_pf), "coord": co.min_pf, "exit": ex.min_pf, "dca": dc.min_pf}
    own = {"minPf": 1.15, "exitMinPf": 1.12, "dcaMinPf": 1.2}
    got = {"default": gates({}), "base 1.4": gates({"setMinPf": 1.4, **own}), "unlinked": gates({"setMinPf": 1.4, "minPfLinked": False, **own})}
    want = {"default": {k: 1.25 for k in got["default"]}, "base 1.4": {k: 1.4 for k in got["default"]},
            "unlinked": {"main": 1.4, "coord": 1.15, "exit": 1.12, "dca": 1.2}}
    return got == want, f"{got}"


def pre_history_grows_when_too_many_sets_lack_orders():
    """When a quarter of the Sets that traded have fewer orders than the gate minimum, the lookback (the pre-history the calcs read) grows by
    240 bars up to histLookbackMax; it never shrinks, survives a reload, and histLookbackAuto off keeps the configured value."""
    from set_engine import LOOKBACK_GROW_STEP
    def after(passes=1, **ov):
        b = _book(n_syms=2, bars=500, setGateMinTrades=400, histLookbackBars=500, **ov)     # nobody can reach 400 orders: every Set that traded is short
        l0 = b.lookback
        for _ in range(passes):
            b.replay_all(now=T0)
        return b, l0
    b, l0 = after(1)
    grown1 = b.lookback
    b2, _ = after(1, histLookbackAuto=False)
    b3, _ = after(30, histLookbackMax=1000)
    b.load(overlay(setGateMinTrades=400, histLookbackBars=500))         # a live-config reload keeps what was grown
    kept = b.lookback
    ok = l0 == 500 and grown1 == l0 + LOOKBACK_GROW_STEP and b2.lookback == 500 and b3.lookback == 1000 and kept == grown1
    return ok, f"start {l0} -> {grown1} after one pass; auto off {b2.lookback}; capped at histLookbackMax {b3.lookback}; after a reload {kept}"


def no_gate_asks_less_than_pf_1_1():
    """MIN_PF_FLOOR (1.1): whatever is configured (setMinPf 1.0, a Short Range floor of 0.9, unlinked Exit / DCA / coordinator values of
    1.0 or 0.5, Block at a small volume ratio), no validation gate asks less than a net PF of 1.1; the desk parses with the same floor."""
    import os, re, tempfile
    from block_engine import BlockBook, calculate_block_minimum_profit_factor
    from coord_engine import Coordinator
    from dca_engine import DcaBook
    from exit_engine import ExitBook
    from position_cost import MIN_PF_FLOOR
    low = {"setMinPf": 1.0, "shortRangeMinPf": 0.9, "minPfLinked": False, "minPf": 1.0, "exitMinPf": 0.5, "dcaMinPf": 1.0}
    sb = SetBook(); sb.load(overlay(stratShortRange=True, **low))
    co = Coordinator(); co.load({}, low)
    ex = ExitBook(); ex.load(low)
    dc = DcaBook(); dc.load(low)
    linked = SetBook(); linked.load(overlay(setMinPf=0.8))
    got = {"set": sb.min_pf, "short": sb.pack_min_pf.get("shortrange"), "coord": co.min_pf, "exit": ex.min_pf, "dca": dc.min_pf,
           "linked": linked.min_pf, "block": calculate_block_minimum_profit_factor(1.25, 0.2, 0.25)}
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    desk = open(os.path.join(root, "src", "lib", "config-model.ts"), encoding="utf-8").read()
    m = re.search(r"export const MIN_PF_FLOOR = ([0-9.]+);", desk)
    ok = MIN_PF_FLOOR == 1.1 and all(v is not None and v + 1e-12 >= 1.1 for v in got.values()) and bool(m) and float(m.group(1)) == MIN_PF_FLOOR
    return ok, f"{got}; desk floor {m and m.group(1)}"


def stage_pools_count_every_set_that_passed_base():
    """With the stages on, pfBase.baseOnly (and validatedBasePct, processed, stageSets.base) holds every Set that passed Base, also one
    that stopped at the Main Auto window or at Real (reasons main, mainauto, real), recomputed here with the later stages switched off;
    the Real pool is rebuilt from each executing Set's own Real-Set orders (its last realGateN)."""
    from set_engine import pf_of
    b = _book(n_syms=4, bars=1200, mainGateN=8, setGateShortN=0, realGateN=5, **SMALL)     # short windows: some Sets reach Real
    b.min_pf = 0.3                  # the synthetic tapes lose: a fixture floor (set past load, which floors at 1.1) so the chain fills
    b.replay_all(now=T0)
    passed = [st for st in b.by_idx if b.gate_reason(st) in ("", "main", "mainauto", "real")]
    for st, why in zip([x for x in passed if b.gate_reason(x)][:3], ("mainauto", "real", "main")):          # one Set stopped at each later stage
        st.stage = {"pos": False, "negl": False, "main": why != "main", "auto": why == "real", "neg": False}
    saved = (b.main_on, b.neg_on, b.realst_on)
    b.main_on = b.neg_on = b.realst_on = False
    base_ok = {st.id for st in b.by_idx if not b.gate_reason(st)}
    b.main_on, b.neg_on, b.realst_on = saved
    reasons = {st.id: b.gate_reason(st) for st in b.by_idx}
    snap = b.snapshot()["pfBase"]
    def rebuilt(lane, k):            # a lane's pool from each executing Set's own Real (k 1) or Real Negative (k 2) Set
        sts = [st for st in b.by_idx if not reasons[st.id] and b.lane_of(st) == lane]
        gp = gl = 0.0
        for st in sts:
            w = b._streams(st)[k][-b.realst_n:]
            gp += sum(x for x in w if x > 0); gl += -sum(x for x in w if x < 0)
        return sts, round(pf_of(gp, gl), 4)
    pos, real_pf = rebuilt("pos", 1)
    neg, neg_pf = rebuilt("neg", 2)
    stopped = sorted({reasons[st.id] for st in passed if reasons[st.id]} & {"main", "mainauto", "real"})
    ok = (len(passed) >= 3 and stopped == ["main", "mainauto", "real"] and snap["baseOnly"]["sets"] == len(base_ok)
          and snap["stageSets"]["base"] == len(base_ok) and snap["validatedBasePct"] == round(100.0 * len(base_ok) / len(b.by_idx), 2)
          and snap["processed"]["sets"] == len(base_ok) and snap["real"]["sets"] == len(pos) and abs(snap["real"]["pf"] - real_pf) < 1e-3 and len(pos) + len(neg) > 0
          and snap["realneg"]["sets"] == len(neg) and abs(snap["realneg"]["pf"] - neg_pf) < 1e-3)
    return ok, (f"Base passed {len(base_ok)} (snapshot {snap['baseOnly']['sets']}, stopped later {stopped}); validated Base {snap['validatedBasePct']}% "
                f"end {snap['validatedPct']}%; Real pool {snap['real']['sets']} Sets PF {snap['real']['pf']} (rebuilt {real_pf}); "
                f"Real Negative {snap['realneg']['sets']} Sets PF {snap['realneg']['pf']} (rebuilt {neg_pf})")


def sets_with_few_orders_are_judged_when_the_minimum_is_off():
    """Replay-level: with setGateMinTrades 0 a Set that closed fewer than 25 orders is valid when its PF, drawdown time and Main gate
    pass; the same book under a minimum of 25 holds none of them and marks the short Sets "n<min". Every Set valid under 25 stays valid
    with the minimum off, and "n<min" is left only to Sets with no closed order."""
    def run(**ov):
        bk = _book(n_syms=2, bars=500, mainGateEnabled=False, stagesEnabled=False, setGateShortN=0, setMinPf=1.0, setMaxDdtHours=36, **ov)   # the minimum alone: no short window
        bk.replay_all(now=T0)
        valid = {st.id for st in bk.by_idx if not bk.gate_reason(st)}
        few = {st.id for st in bk.by_idx if not bk.gate_reason(st) and 0 < st.gate_n < 25}
        short = [st for st in bk.by_idx if bk.gate_reason(st) == "n<min"]
        return valid, few, short
    v0, few0, short0 = run(setGateMinTrades=0)
    v25, few25, short25 = run(setGateMinTrades=25)
    ok = v25 <= v0 and len(few0) > 0 and not few25 and len(short25) > 0 and all(st.gate_n < 1 for st in short0)
    return ok, f"valid off {len(v0)} (with under 25 orders {len(few0)}, n<min {len(short0)}); valid at minimum 25 {len(v25)} (under 25 orders {len(few25)}, n<min {len(short25)})"


CHECKS = [
    ("processing.sim-gate-equals-the-engine-for-every-window-configuration", sim_gate_equals_the_engine_for_every_window_configuration),
    ("processing.stage-figures-average-the-valid-sets-and-report-the-validated-share", stage_figures_average_the_valid_sets_and_report_the_validated_share),
    ("processing.few-order-sets-judged-when-the-minimum-is-off", sets_with_few_orders_are_judged_when_the_minimum_is_off),
    ("processing.broken-or-flat-series-not-replayed", broken_or_flat_series_are_not_replayed),
    ("processing.base-lane-lookup-equals-walk", base_lane_lookup_equals_the_walk),
    ("processing.scratch-lookup-keeps-exact-level", scratch_lookup_keeps_the_exact_level),
    ("processing.consistency-both-halves", a_set_must_pass_on_both_halves_of_its_window_when_asked),
    ("processing.legacy-pick-prefers-judged-sets", legacy_pick_prefers_judged_sets_over_a_single_lucky_order),
    ("processing.step-adapt-waits-for-the-pass", step_adapt_waits_for_the_pass_in_flight),
    ("processing.short-range-trailing-inside-its-steps", short_range_trailing_stays_inside_its_steps),
    ("processing.effective-pf-rolling-window", effective_pf_uses_the_rolling_window_not_the_whole_tape),
    ("processing.short-range-own-pf-floor", short_range_has_its_own_pf_floor),
    ("processing.base-gate-always-on", the_base_gate_cannot_be_switched_off),
    ("processing.pf-progress-history", pf_progress_history_is_continuous_and_bounded),
    ("processing.pack-change-keeps-memo-sound", pack_change_does_not_poison_the_signal_memo),
    ("processing.signal-memo-one-entry-per-bar", the_signal_memo_holds_one_entry_per_bar_for_every_pack),
    ("processing.effective-pf-pooled-over-valid-sets", effective_pf_is_pooled_over_valid_sets_only),
    ("processing.pick-any-no-gap-between-lookups", pick_any_has_no_gap_between_lookups),
    ("processing.departed-symbols-leave-caches", departed_symbols_leave_every_cache),
    ("processing.snapshot-waits-for-writer", snapshot_waits_for_a_writer_mid_update),
    ("processing.rebuild-mid-pass-no-partial-tape", rebuild_mid_pass_scores_no_partial_tape),
    ("processing.live-close-stays-counted-through-replay", live_close_stays_counted_through_a_replay),
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
    ("processing.hold-auto-picks-each-sets-own-best-hold-from-its-last-orders", hold_auto_picks_each_sets_own_best_hold_from_its_last_orders),
    ("processing.hold-auto-replay-assigns-holds-and-judges-the-chosen-tape", hold_auto_replay_assigns_holds_and_judges_the_chosen_tape),
    ("processing.validation-windows-follow-overall-n-floor-8-gates-only", validation_windows_follow_the_overall_n_floor_8_on_the_gates_only),
    ("processing.real-gate-per-set-at-the-base-minimum-pf", real_gate_is_per_set_at_the_base_minimum_pf),
    ("processing.no-gate-asks-less-than-pf-1.1", no_gate_asks_less_than_pf_1_1),
    ("processing.stage-pools-count-every-set-that-passed-base", stage_pools_count_every_set_that_passed_base),
    ("processing.every-validation-gate-asks-the-base-minimum-pf", every_validation_gate_asks_the_base_minimum_pf),
    ("processing.pre-history-grows-when-sets-lack-orders", pre_history_grows_when_too_many_sets_lack_orders),
    ("processing.live-close-once-and-gate", memo.memoized(live_close_counted_once_and_gate_updates)),
    ("processing.concurrent-replay-and-live-closes", concurrent_replay_and_live_closes),
    ("processing.ready-carried-across-refresh", ready_carried_across_refresh),  # needs per-bar callbacks: unmemoized
    ("processing.out-of-grid-sets-retired-and-closes-counted", memo.memoized(out_of_grid_sets_retired_and_closes_counted)),
    ("processing.unmatched-pnl-pct-skipped-not-a-loss", memo.memoized(unmatched_pnl_pct_is_skipped_not_a_loss)),
]
