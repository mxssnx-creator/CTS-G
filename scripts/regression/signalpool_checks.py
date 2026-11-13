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


CHECKS = [("signalpool.parallel-signals-equal-sequential", parallel_signals_equal_sequential)]
