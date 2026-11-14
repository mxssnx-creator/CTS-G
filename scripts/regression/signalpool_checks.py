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


CHECKS = [("signalpool.parallel-signals-equal-sequential", parallel_signals_equal_sequential),
          ("signalpool.signal-memo-survives-a-restart", memo_survives_a_restart)]
