"""Indication suite: causal signals, the timeframe lanes and their overlay keys, and the combined vote.

The wiring check reads the settings the set signal really receives. It guards a defect found by the
indication sweep: tf5m / tf15m / tfCombined / tfMinAgree were not in the set engine's indication settings.
"""
from __future__ import annotations

from common import T0, overlay, synth_bars  # noqa: F401
from indication_engine import DEFAULT_SETTINGS, aggregate_bars, combine_timeframes, timeframe_evals
from set_engine import SetBook, indication_signal, pack_signals


def causal_signals_ignore_the_future():
    b = SetBook()
    b.load(overlay())
    bars = synth_bars(5, 420, drift=0.0003)
    m = 330
    full = pack_signals(bars, b.packs, b.ind_settings, T0, 30)
    head = pack_signals(bars[:m], b.packs, b.ind_settings, T0, 30)
    bad = [(p, i) for p in b.packs for i in range(30, m) if full[p][i] != head[p][i]]
    return not bad, f"packs={len(b.packs)} bars_compared={m - 30} mismatches={len(bad)}"


def timeframe_keys_reach_the_set_signal():
    ov = overlay(tfMinAgree=3, tf5m=False, tf15m=True, tfCombined=False)
    b = SetBook()
    b.load(ov)
    got = {k: b.ind_settings.get(k) for k in ("tfMinAgree", "tf5m", "tf15m", "tfCombined")}
    want = {"tfMinAgree": 3, "tf5m": False, "tf15m": True, "tfCombined": False}
    ok = got == want
    return ok, f"set signal sees {got} want {want}"


def timeframe_flag_removes_the_lane():
    settings = dict(DEFAULT_SETTINGS)
    settings["tf5m"] = False
    seen = set()
    for seed in range(12):
        bars = synth_bars(40 + seed, 400, drift=0.0006)
        rows = {"1m": bars[-60:], "5m": aggregate_bars(bars, 5), "15m": aggregate_bars(bars, 15)}
        for ev in timeframe_evals(rows, settings, T0):
            seen.add(ev.source_id)
    ok = "bingx-5m" not in seen
    return ok, f"lanes seen with tf5m off: {sorted(seen) or 'none'}"


def combined_vote_needs_min_agree():
    bars = synth_bars(77, 400, drift=0.0008)
    rows = {"1m": bars[-60:], "5m": aggregate_bars(bars, 5), "15m": aggregate_bars(bars, 15)}
    evs = timeframe_evals(rows, dict(DEFAULT_SETTINGS), T0)
    too_many = combine_timeframes(evs, len(evs) + 1, dict(DEFAULT_SETTINGS))
    ok = too_many is None
    return ok, f"lanes={len(evs)} min_agree={len(evs) + 1} -> {too_many}"


def match_names_kind_and_mode():
    """An entry reason ind:<kind>:<mode>:... names one row. A row with the right kind but the other mode is not it,
    and the mode decides the direction."""
    from indication_engine import Indication, IndicationBook

    def row(kind, mode, direction):
        return Indication(symbol="S", direction=direction, mode=mode, confidence=0.7, strength=0.5, agreement=0.6,
                          stop_loss_pct=0.01, take_profit_pct=0.02, reward_risk=2.0, last_price=1.0, sources=["a"],
                          votes_long=1, votes_short=0, primary=False, t=T0, timeframe="1m", kind=kind)

    book = IndicationBook()
    book.last = {"S": [row("state", "tf_combined", "long"), row("state", "multi_source_consensus", "short")]}
    short_row = book.match("S", "ind:state:multi_source_consensus:0.60:a2:src")
    long_row = book.match("S", "ind:state:tf_combined:0.60:a2:src")
    ok = (short_row is not None and short_row.mode == "multi_source_consensus" and short_row.direction == "short"
          and long_row is not None and long_row.mode == "tf_combined" and long_row.direction == "long")
    return ok, f"named_short={getattr(short_row, 'direction', None)} named_long={getattr(long_row, 'direction', None)}"


def snapshot_survives_concurrent_writes():
    """The warm thread writes the book while the main thread takes snapshots: no reader may raise."""
    import threading
    from indication_engine import Indication, IndicationBook

    def row(sym, kind):
        return Indication(symbol=sym, direction="long", mode="tf_combined", confidence=0.7, strength=0.5, agreement=0.6,
                          stop_loss_pct=0.01, take_profit_pct=0.02, reward_risk=2.0, last_price=1.0, sources=["a"],
                          votes_long=1, votes_short=0, primary=True, t=T0, timeframe="1m", kind=kind)

    book = IndicationBook()
    errors, reads = [], []
    stop = threading.Event()

    def writer():
        k = 0
        while not stop.is_set() and k < 20000:
            sym = f"S{k % 50}"
            book.last[sym] = [row(sym, "state")]
            book.last.pop(f"S{(k + 25) % 50}", None)
            k += 1

    def reader():
        try:
            for _ in range(1500):
                book.snapshot()
                reads.append(1)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
        finally:
            stop.set()

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    w.start(); r.start()
    r.join(60); w.join(60)
    ok = not errors and not r.is_alive() and len(reads) > 0
    return ok, f"reads={len(reads)} errors={errors[:1]}"


def signal_is_deterministic():
    b = SetBook()
    b.load(overlay())
    bars = synth_bars(9, 400, drift=0.0003)
    a = pack_signals(bars, b.packs, b.ind_settings, T0, 30)
    c = pack_signals(bars, b.packs, b.ind_settings, T0, 30)
    return a == c, "two runs identical" if a == c else "differs between runs"


# A window where the Set pack gives all four votes with every switch on (found by search, fixed here).
SWITCH_BARS = (48, 0.0003)
SWITCH_TOKEN = {"indTypeSignals": "tf", "indTypeState": "ta", "indTypeDirection": "dir", "indTypeMove": "move"}


def set_switches_reach_the_set_signal():
    """The timeframe and type switches IndicationBook reads are forwarded to the Set pack's settings, unchanged."""
    off = {"tf1m": False, "indTypeState": False, "indTypeDirection": False, "indTypeMove": False,
           "indTypeSignals": False}
    b = SetBook()
    b.load(overlay(**off))
    got = {k: b.ind_settings.get(k) for k in ("tf1m", "typeState", "typeDirection", "typeMove", "typeSignals")}
    want = {"tf1m": False, "typeState": False, "typeDirection": False, "typeMove": False, "typeSignals": False}
    return got == want, f"set signal sees {got}"


def switched_off_vote_is_absent():
    """Each type switch removes its own vote from the Set pack, and the other votes stay."""
    seed, drift = SWITCH_BARS
    bars = synth_bars(seed, 400, drift=drift)[-420:]
    on = SetBook()
    on.load(overlay())
    _, _, why_on = indication_signal(bars, on.ind_settings, T0)
    all_votes = set(why_on.split("+"))
    if all_votes != {"tf", "ta", "dir", "move"}:
        return False, f"precondition: the window must give all four votes, got {why_on!r}"
    problems = []
    for switch, token in SWITCH_TOKEN.items():
        book = SetBook()
        book.load(overlay(**{switch: False}))
        _, _, why = indication_signal(bars, book.ind_settings, T0)
        got = set(why.split("+")) - {""}
        if token in got or not got <= all_votes:
            problems.append(f"{switch}=off gave {why!r}")
    return not problems, "; ".join(problems) or "each switch removes exactly its own vote"


def all_switches_off_is_flat():
    """With every vote switched off the Set pack has no vote, so the signal is flat."""
    seed, drift = SWITCH_BARS
    bars = synth_bars(seed, 400, drift=drift)[-420:]
    book = SetBook()
    book.load(overlay(indTypeState=False, indTypeDirection=False, indTypeMove=False, indTypeSignals=False))
    direction, _conf, why = indication_signal(bars, book.ind_settings, T0)
    return direction == 0 and why == "flat", f"direction={direction} why={why!r}"


def timeframe_1m_switch_removes_the_lane_from_the_set_pack():
    """tf1m off removes the 1m lane from the timeframe evaluations the Set pack uses."""
    seed, drift = SWITCH_BARS
    bars = synth_bars(seed, 400, drift=drift)
    rows = {"1m": bars[-60:], "5m": aggregate_bars(bars, 5), "15m": aggregate_bars(bars, 15)}
    book = SetBook()
    book.load(overlay(tf1m=False))
    seen = {ev.source_id for ev in timeframe_evals(rows, book.ind_settings, T0)}
    on = SetBook()
    on.load(overlay())
    seen_on = {ev.source_id for ev in timeframe_evals(rows, on.ind_settings, T0)}
    ok = "bingx-1m" in seen_on and "bingx-1m" not in seen and "bingx-5m" in seen
    return ok, f"lanes on={sorted(seen_on)} with tf1m off={sorted(seen)}"


CHECKS = [
    ("indication.causal-signals-ignore-future", causal_signals_ignore_the_future),
    ("indication.timeframe-keys-reach-set-signal", timeframe_keys_reach_the_set_signal),
    ("indication.timeframe-flag-removes-lane", timeframe_flag_removes_the_lane),
    ("indication.combined-vote-needs-min-agree", combined_vote_needs_min_agree),
    ("indication.match-names-kind-and-mode", match_names_kind_and_mode),
    ("indication.snapshot-survives-concurrent-writes", snapshot_survives_concurrent_writes),
    ("indication.signal-deterministic", signal_is_deterministic),
    ("indication.set-switches-reach-the-set-signal", set_switches_reach_the_set_signal),
    ("indication.switched-off-vote-is-absent", switched_off_vote_is_absent),
    ("indication.all-switches-off-is-flat", all_switches_off_is_flat),
    ("indication.tf1m-switch-removes-the-set-lane", timeframe_1m_switch_removes_the_lane_from_the_set_pack),
]
