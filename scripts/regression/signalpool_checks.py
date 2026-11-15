"""The worker-process signal pool computes exactly the signals the engine's own process computes (opt-in speed-up of a Set rebuild)."""
from common import overlay, synth_bars
from set_engine import SetBook, pack_signals
import signal_pool

SMALL_PACKS = {"stratShortRange": True, "stratTrend": True, "stratBreakout": True, "stratReversion": True, "stratFlow": True, "stratMulti": True}


def parallel_signals_equal_sequential():
    """Every bar of every pack: the pool's rows (windows cut from a slice that starts one signal window early) equal the in-process rows, with and without the memo cache,
    for a short history (below the pool's minimum: in-process path) and a longer one (pool path); a pool that cannot run returns None and the caller computes alone."""
    b = SetBook()
    b.load(overlay(**{"holdEnabled": False, "setStepAdapt": False, **SMALL_PACKS}))
    try:
        results = []
        for n in (60, 220):
            bars = synth_bars(5, n, drift=.0004)
            seq = pack_signals(bars, b.packs, b.ind_settings, 1.7e9, 30)
            par = pack_signals(bars, b.packs, b.ind_settings, 1.7e9, 30, workers=2)
            cache = {}
            first = pack_signals(bars, b.packs, b.ind_settings, 1.7e9, 30, cache=cache, salt=3, workers=2)
            second = pack_signals(bars, b.packs, b.ind_settings, 1.7e9, 30, cache=cache, salt=3, workers=2)
            results.append((seq == par, seq == first, seq == second))
        direct = signal_pool.compute(synth_bars(5, 220, drift=.0004), list(range(30, 220)), b.packs, b.ind_settings, 1.7e9, 900, workers=2)
        off = signal_pool.compute(synth_bars(5, 220, drift=.0004), list(range(30, 220)), b.packs, b.ind_settings, 1.7e9, 900, workers=0)
        small = signal_pool.compute(synth_bars(5, 220, drift=.0004), [40, 41], b.packs, b.ind_settings, 1.7e9, 900, workers=2)
    finally:
        signal_pool.shutdown()
    ok = all(all(r) for r in results) and direct is not None and len(direct) == 190 and off is None and small is None
    return ok, f"equal (plain, cache first, cache second) for n=60 and n=220: {results}; pool rows {None if direct is None else len(direct)}, off {off}, below minimum {small}"


def memo_survives_a_restart():
    """The signal memo kept on disk (overlay signalCachePath) answers exactly what a cold replay computes: a second process-equivalent SetBook with the same settings reads the saved
    windows and computes only the bars that are new, a book with other settings (another salt) reads nothing, and the Sets' rows are identical with and without the file."""
    import os, tempfile, time
    import set_engine as se
    from correctness import SMALL, T0
    calls = {"n": 0}
    real = se.bar_signal

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    def run(path, extra_bars=0, **kw):
        b = SetBook()
        b.load(overlay(**{**SMALL, "holdEnabled": False, "setStepAdapt": False, **SMALL_PACKS, "signalCachePath": path, **kw}))
        for i in range(2):
            sym = f"M{i}-USDT"
            b.bars[sym] = synth_bars(i, 360 + extra_bars, drift=.0004)
            b.bar_t[sym] = T0 - 60
        calls["n"] = 0
        b.replay_all(now=T0)
        rows = {st.id: len(st.hist) for st in b.by_idx[:60]}
        return b, calls["n"], rows
    se.bar_signal = counting
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sig.pkl")
            cold, n_cold, rows_cold = run(path)
            cold._save_sig_cache_maybe(force=True)
            for _ in range(100):
                if os.path.exists(path):
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            warm, n_warm, rows_warm = run(path)
            other, n_other, _ = run(path, stratMulti=False)          # other packs, other salt: the file must not be read (a gate setting such as setMinPf is not part of a signal and rightly keeps the memo)
            plain, n_plain, rows_plain = run("")
    finally:
        se.bar_signal = real
    ok = n_cold > 0 and n_warm == 0 and n_other == n_cold and n_plain == n_cold and rows_warm == rows_cold == rows_plain
    return ok, f"bar signals computed: cold {n_cold}, warm from the file {n_warm}, other settings {n_other}, no file {n_plain}; Set rows equal {rows_warm == rows_cold == rows_plain}"


def forked_lane_scans_equal_the_in_thread_scans():
    """scanWorkers (2026-11-15): the per-Set lane scans of a symbol run in forked children. Every Set's stored columns (each row), which keys share
    columns (tape aliases), the pause choices, the verdicts and lanes equal the in-thread replay with the pause Auto on and split directions; a fork that
    fails, and a child that dies, fall back to the in-thread scan with the same result."""
    import os
    import set_engine as se
    from correctness import SMALL, T0

    def run(workers, **kw):
        b = SetBook()
        b.load(overlay(**{**SMALL, "holdEnabled": False, "setStepAdapt": False, "stratTrend": True, "stratReversion": True, "pauseMode": "auto",
                          "scanWorkers": workers, **kw}))
        for i in range(3):
            sym = f"F{i}-USDT"
            b.bars[sym] = synth_bars(20 + i, 700, drift=.0003 * (1 - i), vol=.004)
            b.bar_t[sym] = T0 - 60
        b.replay_all(now=T0)
        rows = {(s, sid): list(cols) for s, d in b._hist_rows.items() for sid, cols in d.items()}
        shared = sorted((s, a, c) for s, d in b._hist_rows.items() for a, x in d.items() for c, y in d.items() if a < c and x.t is y.t)
        verdicts = {st.id: (b.lane_of(st), st.gate_pf, st.gate_n, st.main_pf, st.hist) for st in b.by_idx}
        return rows, shared, dict(b.pause_now), verdicts, len(b.sets)
    real_sig, real_min, real_fork, real_share = se.pack_signals, se.SCAN_FORK_MIN_SETS, os.fork, se._scan_share
    memo = {}

    def once(bars, packs, *a, **k):              # the four replays read the same signals: compute them once (the scan is what is compared)
        key = (len(bars), tuple(bars[-1]), tuple(bars[0]), tuple(packs))
        if key not in memo:
            memo[key] = real_sig(bars, packs, *a, **k)
        return memo[key]

    def no_fork():
        raise OSError("no fork")

    def dies(*a, **k):
        raise RuntimeError("child dies")
    real_split = se._scan_shares
    split = []

    def counted(*a, **k):
        got = real_split(*a, **k)
        split.append(len(got))
        return got
    try:
        se._scan_shares = counted
        se.pack_signals, se.SCAN_FORK_MIN_SETS = once, 8      # several shares per symbol, so a pack is also cut across children
        plain = run(0)
        forked = run(3)
        os.fork = no_fork
        nofork = run(3)
        os.fork = real_fork
        se._scan_share = dies
        died = run(3)
    finally:
        se.pack_signals, se.SCAN_FORK_MIN_SETS, os.fork, se._scan_share, se._scan_shares = real_sig, real_min, real_fork, real_share, real_split
    names = ("rows", "aliases", "pauses", "verdicts")
    eq = {nm: (plain[i] == forked[i], plain[i] == nofork[i], plain[i] == died[i]) for i, nm in enumerate(names)}
    ok = (all(all(v) for v in eq.values()) and len(plain[0]) > 100 and len(plain[1]) > 0 and any(v for v in plain[2].values())
          and split and min(split[:3]) >= 2)
    return ok, (f"{plain[4]} Sets, {len(plain[0])} (symbol, Set) tapes, {len(plain[1])} shared, {sum(1 for v in plain[2].values() if v)} pauses chosen; "
                f"shares per symbol {split[:3]}; equal (forked, fork fails, child dies): {eq}")


def the_stats_snapshot_is_cached_only_when_it_is_costly():
    """SetBook.snapshot_cached (2026-11-15, x02: a 0.9 s snapshot rebuilt every second under the book's lock): a cheap snapshot is built on every call (a
    change shows at once); a costly one is served from the last build with the live progress, rebuilt in the background after its interval, and marked for a
    rebuild by a scoring pass; what it serves is a snapshot the book really built."""
    import time as _t
    import set_engine as se
    from correctness import SMALL, T0
    b = SetBook()
    b.load(overlay(**{**SMALL, "holdEnabled": False}))
    b.bars["C-USDT"] = synth_bars(4, 300, drift=.0004)
    b.bar_t["C-USDT"] = T0 - 60
    b.replay_all(now=T0)
    cheap1 = b.snapshot_cached()
    b.progress.detail = "changed-1"
    cheap2 = b.snapshot_cached()
    real_min, real_x = se.SNAP_CACHE_MIN_COST_S, se.SNAP_CACHE_X
    try:
        se.SNAP_CACHE_MIN_COST_S, se.SNAP_CACHE_X = 0.0, 1e9          # every snapshot counts as costly; the interval never ends by itself
        first = b.snapshot_cached()
        built = b._snap_cache["snap"]
        b.progress.detail = "changed-2"
        served = b.snapshot_cached()
        cached_ok = served["rows"] is built["rows"] and served["progress"]["detail"] == "changed-2"
        b._snapshot_stale()
        during = b.snapshot_cached()                 # starts the background rebuild, serves the old one meanwhile
        for _ in range(200):
            if b._snap_cache["snap"] is not built and not b._snap_cache["busy"]:
                break
            _t.sleep(0.02)
        after = b.snapshot_cached()
        rebuilt = b._snap_cache["snap"] is not built and after["rows"] == b.snapshot()["rows"]
    finally:
        se.SNAP_CACHE_MIN_COST_S, se.SNAP_CACHE_X = real_min, real_x
    ok = (cheap2["progress"]["detail"] == "changed-1" and cheap2["rows"] is not cheap1["rows"] and cached_ok and during["rows"] is built["rows"]
          and rebuilt and first["snapshotAgeS"] == 0.0)
    return ok, f"cheap rebuilt each call {cheap2['rows'] is not cheap1['rows']}; costly served with live progress {cached_ok}; rebuilt after a pass {rebuilt}"


CHECKS = [("signalpool.parallel-signals-equal-sequential", parallel_signals_equal_sequential),
          ("signalpool.signal-memo-survives-a-restart", memo_survives_a_restart),
          ("processing.forked-lane-scans-equal-the-in-thread-scans", forked_lane_scans_equal_the_in_thread_scans),
          ("processing.the-stats-snapshot-is-cached-only-when-it-is-costly", the_stats_snapshot_is_cached_only_when_it_is_costly)]
