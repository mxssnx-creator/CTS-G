"""Signals as Set-engine packs (owner 2026-11-14): every (source, range, timeframe) of signal_sources is its own pack sig-<source>-<range>-t<tf> that runs the
same Sets, stage chain, last-N windows and lanes as every other pack."""
from common import SMALL, T0, overlay, synth_bars  # noqa: F401  (common sets sys.path)
import signal_packs as sp
import signal_sources as ss
import set_engine as se


def _rows_ms(bars, close_last):
    """[t_ms, o, h, l, c, v] rows of 1m bars whose last bar closes at close_last (seconds)."""
    n = len(bars)
    return [(int(round((close_last - 60.0 * (n - k)) * 1000.0)),) + tuple(float(x) for x in b) for k, b in enumerate(bars)]


def the_signal_is_the_sources_onset_on_its_own_window():
    """On every 1m bar the pack's signal equals signal_sources.onsets of the same 1m rows the pack reads (its window), at the bar that completes a timeframe bar,
    and nothing on the bars in between: 9 sources x 2 ranges x 4 timeframes on a synthetic walk."""
    bars = synth_bars(7, 900, drift=0.0002, vol=0.003)
    close0 = float(int(T0 // 1800) * 1800)                  # the first bar closes on a 30-minute boundary
    bad, fired = [], 0
    for src in ss.SOURCES:
        for rng in ss.RANGES:
            for tf in ss.TIMEFRAMES:
                pk = sp.pack_name(src, rng, tf)
                w = sp.window_minutes(pk)
                for i in range(1, len(bars), 3 if tf == 1 else 1):
                    close = close0 + 60.0 * i
                    got = sp.signal(bars, i, close, pk)
                    lo = max(0, i + 1 - w)
                    want = ss.onsets(_rows_ms(bars[lo:i + 1], close), src, rng, tf).get(int(round(close * 1000.0)), 0)
                    if got[0] != want:
                        bad.append((pk, i, got[0], want))
                    fired += bool(got[0])
    return not bad and fired > 50, f"{fired} signals over 72 packs, mismatches {len(bad)} {bad[:3]}"


def the_signal_never_reads_a_later_bar():
    """Appending bars after bar i (even absurd ones) never changes the signal at i."""
    bars = synth_bars(11, 700, vol=0.004)
    close0 = float(int(T0 // 1800) * 1800)
    later = bars + [[b[3], b[3] * 3, b[3] * 0.3, b[3] * 2, 9e9] for b in bars[-5:]]
    bad = []
    for pk in (sp.pack_name("ema-cross", "short", 5), sp.pack_name("donchian", "medium", 15), sp.pack_name("rsi-reversal", "short", 1)):
        for i in range(1, len(bars)):
            if sp.signal(bars, i, close0 + 60.0 * i, pk) != sp.signal(later, i, close0 + 60.0 * i, pk):
                bad.append((pk, i))
    return not bad, f"changed by later bars: {bad[:3]}"


def every_signal_pack_is_its_own_set_grid_at_the_signals_floor():
    """stratSignals + signalConfigs: each (source, range, timeframe) is one pack with its entry's step range, SL ratios and types; its Sets ask minPfSignals;
    an unknown source is dropped and listed; off = no Signals pack."""
    cfg = [{"sources": ["ema-cross", "donchian", "nope"], "ranges": ["short"], "timeframes": [5, 15], "stepMin": 6, "stepMax": 9, "slRatios": [0.5, 1.0],
            "types": ["base", "trail"]}]
    b = se.SetBook()
    b.load(overlay(**{**SMALL, "stratSignals": True, "signalConfigs": cfg, "stratIndications": False, "stratGeneral": False}))
    sig = [p for p in b.packs if p.startswith(sp.PREFIX)]
    sets = [st for st in b.by_idx if st.pack in sig]
    steps = {st.step for st in sets if st.kind == "base"}
    ratios = {round(st.sl_ratio, 2) for st in sets if st.kind == "base"}
    kinds = {st.kind for st in sets}
    floors = {b.floor_for(20, st.pack, st) for st in sets}
    off = se.SetBook()
    off.load(overlay(**{**SMALL, "stratSignals": False, "signalConfigs": cfg}))
    dropped = [d["name"] for d in b.signal_report["dropped"]]
    ok = (sorted(sig) == sorted(sp.pack_name(s, "short", tf) for s in ("ema-cross", "donchian") for tf in (5, 15)) and steps == {6, 7, 8, 9}
          and ratios == {0.5, 1.0} and kinds <= {"base", "trail"} and "exit" not in kinds and floors == {b.signal_min_pf} and b.signal_min_pf >= 2.2
          and any("nope" in d for d in dropped) and not any(p.startswith(sp.PREFIX) for p in off.packs) and all(se.is_signal_pack(p) for p in sig))
    return ok, f"packs {sig}; steps {sorted(steps)}; ratios {sorted(ratios)}; kinds {sorted(kinds)}; floors {sorted(floors)}; dropped {dropped}"


def a_signal_pack_reads_only_its_own_source():
    """The replay's signals of one Signals pack are identical whether it runs alone or next to every other pack (indications, general, the other Signals packs)."""
    bars = synth_bars(5, 1200, drift=0.0003, vol=0.003)
    base_ts = float(int(T0 // 1800) * 1800)
    pk = sp.pack_name("macd-cross", "short", 5)
    others = ["indications", "general"] + [sp.pack_name(s, "medium", 15) for s in ("ema-trend", "obv-trend")]
    b = se.SetBook()
    b.load(overlay(**SMALL))
    settings = dict(b.ind_settings)
    alone = se.pack_signals(bars, [pk], settings, base_ts, 50)[pk]
    mixed = se.pack_signals(bars, [pk] + others, settings, base_ts, 50)[pk]
    fired = sum(1 for x in alone if x[0])
    return alone == mixed and fired > 0, f"{fired} signals of {pk}; identical next to {len(others)} other packs: {alone == mixed}"


def blocks_equal_completed_bars():
    """The fast aggregation (signal_packs._blocks, slices) gives exactly signal_sources.completed_bars on contiguous minutes, for every timeframe and start phase."""
    bars = synth_bars(13, 400, vol=0.003)
    bad = []
    for start in range(0, 31):
        close = float(int(T0 // 1800) * 1800) + 60.0 * (399 + start)
        rows = _rows_ms(bars, close)
        for tf in (5, 15, 30):
            if sp._blocks(rows, tf) != ss.completed_bars(rows, tf):
                bad.append((start, tf))
    return not bad, f"phases x timeframes differing: {bad[:5]}"


CHECKS = [
    ("signals-pack.blocks-equal-completed-bars", blocks_equal_completed_bars),
    ("signals-pack.the-signal-is-the-sources-onset-on-its-own-window", the_signal_is_the_sources_onset_on_its_own_window),
    ("signals-pack.the-signal-never-reads-a-later-bar", the_signal_never_reads_a_later_bar),
    ("signals-pack.every-signal-pack-is-its-own-set-grid-at-the-signals-floor", every_signal_pack_is_its_own_set_grid_at_the_signals_floor),
    ("signals-pack.a-signal-pack-reads-only-its-own-source", a_signal_pack_reads_only_its_own_source),
]
