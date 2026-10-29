"""Indication suite: causal signals, the timeframe lanes and their overlay keys, and the combined vote.

The wiring check reads the settings the set signal really receives. It guards a defect found by the
indication sweep: tf5m / tf15m / tfCombined / tfMinAgree were not in the set engine's indication settings.
"""
from __future__ import annotations

from common import SMALL, T0, overlay, synth_bars  # noqa: F401
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
    import sys
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)          # switch threads constantly: a reader that iterates the live dict is then caught every run
    try:
        w.start(); r.start()
        r.join(60); w.join(60)
    finally:
        sys.setswitchinterval(old)
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


def extra_book_fetches_a_key_once_while_in_flight():
    """Two callers ask for the same venue key while the first fetch is still running. The venue must see one request,
    and both callers must get the same result."""
    import threading
    import time as _time
    from indication_engine import ExtraBook

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class _Http:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()
            self.entered = threading.Event()
            self.release = threading.Event()

        def get(self, url, *a, **k):
            with self.lock:
                self.calls += 1
            self.entered.set()
            self.release.wait(10)
            return _Resp()

    book = ExtraBook()
    book.http = _Http()
    out = {}
    first = threading.Thread(target=lambda: out.__setitem__("a", book.get("binance-usdm", "BTC-USDT")), name="first")
    first.start()
    if not book.http.entered.wait(10):
        return False, "the first fetch never started"
    second = threading.Thread(target=lambda: out.__setitem__("b", book.get("binance-usdm", "BTC-USDT")), name="second")
    second.start()
    _time.sleep(0.2)                         # the second caller reaches get() while the first is still in flight
    book.http.release.set()
    first.join(10)
    second.join(10)
    calls = book.http.calls
    ok = calls == 1 and not first.is_alive() and not second.is_alive() and "a" in out and "b" in out
    return ok, f"venue requests for one in-flight key={calls}; both callers returned={'a' in out and 'b' in out}"


def bars_to_entry_reason_round_trip():
    """Indications end to end: bars go through process, pick_entry chooses the row, the entry reason is built the way the
    trader builds it, and match returns that same row. A row switched off by its type switch cannot be picked."""
    from indication_engine import IndicationBook

    bars = synth_bars(48, 400, drift=0.0003)
    rows = {"1m": bars[-60:], "5m": aggregate_bars(bars, 5), "15m": aggregate_bars(bars, 15)}

    def run(**ov):
        b = IndicationBook()
        b.load(overlay(**ov))
        b.process("BTC-USDT", bars[-60:], pulse_dir=1, pulse_conf=0.7, px=float(bars[-1][3]), sl_pct=0.005,
                  tp_pct=0.0075, want_extra=False, bars_by_tf=rows)
        return b

    b = run()
    got = b.pick_entry("BTC-USDT", 0.5)
    if got is None:
        return False, "pick_entry found no row on a clear uptrend"
    pick, _conf, agree_n = got
    why = f"ind:{pick.kind}:{pick.mode}:{pick.agreement:.2f}:a{agree_n}:{','.join(pick.sources[:3])}"
    row = b.match("BTC-USDT", why)
    same = row is not None and row.kind == pick.kind and row.mode == pick.mode and row.direction == pick.direction
    off = run(indTypeSignals=False, indTypeState=False, indTypeDirection=False, indTypeMove=False,
              indTypeCommon=False, indTypeActive=False)
    nothing = off.pick_entry("BTC-USDT", 0.0) is None
    return same and nothing, f"picked {pick.kind}/{pick.mode} -> matched={same}; all types off -> no pick={nothing}"


def match_follows_the_timeframe_lane_of_the_pick():
    """Rows from different timeframe lanes share kind and mode. The entry reason ends with the pick's sources, and
    match must return the row of that lane, not the first row of the kind."""
    from indication_engine import Indication, IndicationBook
    def row(direction, conf, src):
        return Indication(symbol="X-USDT", direction=direction, mode="direct_tf", confidence=conf, strength=1.0,
                          agreement=1.0, stop_loss_pct=1.0, take_profit_pct=2.0, reward_risk=2.0, last_price=1.0,
                          sources=[src], votes_long=1, votes_short=0, primary=False, t=T0, timeframe="", kind="signals")
    b = IndicationBook()
    b.last["X-USDT"] = [row("short", 0.65, "bingx-1m"), row("long", 0.70, "bingx-5m"), row("long", 0.80, "bingx-15m")]
    got = b.match("X-USDT", "ind:signals:direct_tf:1.00:a1:bingx-15m")
    return got.direction == "long" and got.sources == ["bingx-15m"], f"matched {got.direction} {got.sources}"


def a_close_without_pnl_pct_is_not_a_loss_in_exits_and_dca():
    from exit_engine import ExitBook
    from dca_engine import DcaBook
    e = ExitBook()
    e.on_close({"t": 1.0, "pnl": -0.1, "reason": "exit:peak"})
    d = DcaBook()
    d.closes = [{"pnl_pct": None}] * 30
    sc = d.score()
    ok = not any(ln.n for ln in e.lanes.values()) and d.active
    return ok, f"exit lanes scored={[ (k, v.n) for k, v in e.lanes.items()]} dca active={d.active}"


def library_memo_never_serves_another_state_of_the_rows():
    """indication_lib memoises the 5m / 15m aggregation per rows object. A history list that grows, shrinks (a truncation would leak
    later bars) or replaces its newest row in place must be aggregated again: every library indicator on the mutated list equals the
    same indicator on a fresh copy; and every signal is well formed ((+1 or -1, confidence 0..1) or None)."""
    from indication_lib import LIB
    import random as _r
    rng = _r.Random(5)
    bars = [[50.0, 50.1, 49.9, 50.0, 1.0]]
    for _ in range(460):
        c = bars[-1][3] * (1 + rng.gauss(0.0002, 0.002))
        o = bars[-1][3]
        bars.append([o, max(o, c) * (1 + abs(rng.gauss(0, 0.0007))), min(o, c) * (1 - abs(rng.gauss(0, 0.0007))), c, rng.uniform(0.3, 3.0)])
    names = sorted(LIB)
    pick = [n for n in names if n.startswith("g_")] + names[::7]
    live = bars[:200]                                   # inside the 300-row window (a longer list is sliced, a new object)
    bad, wrong = [], 0
    steps = [("grow", 260), ("shrink", 230), ("grow", 300), ("replace", None)]
    for k in pick:
        LIB[k](live)                                     # fill the memo with the current state
    for what, n in steps:
        if what == "grow":
            live.extend(bars[len(live):n])
        elif what == "shrink":
            del live[n:]
        else:
            live[-1] = [x * 1.003 for x in live[-1][:4]] + [live[-1][4]]
        got = {k: LIB[k](live) for k in pick}            # the mutated list first, then fresh copies
        for k in pick:
            a, b = got[k], LIB[k](list(live))
            if a != b:
                bad.append((what, k))
            if a is not None and (a[0] not in (-1, 1) or not 0.0 <= a[1] <= 1.0):
                wrong += 1
    return not bad and not wrong, f"{len(pick)} indicators x {len(steps)} in-place changes; stale {len(bad)} {bad[:3]}; malformed {wrong}"


def _regime_bars(seed, n):
    """1m bars with trends, quiet ranges, spikes and varying volume (a constant volume would silence the volume families)."""
    import random as _r
    rng = _r.Random(seed)
    out, c = [], 100.0
    for i in range(n):
        phase = (i // 400) % 4
        drift = (0.0008, 0.0, -0.0008, 0.0)[phase]
        vol = (0.0015, 0.0004, 0.0015, 0.003)[phase]
        o = c
        c = max(1.0, c * (1 + drift + rng.gauss(0, vol)))
        if rng.random() < 0.01:
            c *= 1 + rng.choice((-1, 1)) * 0.01
        out.append([o, max(o, c) * (1 + abs(rng.gauss(0, vol / 2))), min(o, c) * (1 - abs(rng.gauss(0, vol / 2))), c, rng.uniform(0.2, 5.0)])
    return out


def every_name_gets_the_history_its_timeframe_needs():
    """A name on a higher timeframe reads TF_BARS bars of it (indication_lib.rows_for: 480 rows for 5m, 1440 for 15m; combined and
    group votes the 15m history); 124 names could never fire on the 300-row window. The band families (bbb, bbrev, vwapz) use a
    width a close can reach in its own n-bar window (2 sd is out of reach at n = 5); a flat stretch never raises. Names that were
    dead fire on a long series with trends, ranges and spikes."""
    from indication_extra import INDICATORS, window_of
    from indication_lib import _band_k, rows_for
    import math as _m
    bad_w = [k for k in INDICATORS if (k.endswith("_t15") or k.startswith("g_")) and window_of(k) < rows_for(15)]
    bad_w += [k for k in INDICATORS if k.endswith("_t5") and window_of(k) < rows_for(5)]
    reach = all(_band_k(n) < _m.sqrt(n - 1) for n in (5, 8)) and _band_k(14) == 2.0
    flat = [[100.0, 100.0, 100.0, 100.0, 1.0]] * 1500
    raised = []
    for k in ("bbb_n5_t1", "bbb_n8_t1", "bbrev_n5_t1", "vwapz_n5_t1"):
        try:
            INDICATORS[k](flat[-window_of(k):])
        except Exception as e:      # noqa: BLE001
            raised.append((k, repr(e)))
    revived = ("bbb_n5_t1", "bbrev_n5_t1", "vwapz_n5_t1", "vwapz_n8_t1", "adx_n21_t15", "ichi_n14_t15", "trend_n40_t15", "ao_htfpb_n8_t5")
    bars = _regime_bars(3, 6000)
    fired = {k: 0 for k in revived}
    for i in range(1500, len(bars), 7):
        for k in revived:
            if INDICATORS[k](bars[i + 1 - window_of(k):i + 1]):
                fired[k] += 1
    dead = [k for k, v in fired.items() if not v]
    ok = not bad_w and reach and not raised and not dead
    return ok, f"short windows {bad_w[:4]} ({len(bad_w)}); band width reachable={reach}; raised on flat {raised}; fired {fired}"


def the_signal_memo_key_covers_every_bar_a_pack_reads():
    """pack_signals keys its memo on signal_window(packs, settings) bars: a pack whose names read 1440 rows (15m) must not take the
    memo of another bar whose newest 300 rows are equal but whose older history differs."""
    import set_engine as se
    b = SetBook()
    b.load(overlay(stratTrend=True, trendVariants=["trend_n20_t15", "adx_n21_t15", "ichi_n14_t15"], **SMALL))
    A = _regime_bars(11, 3200)
    B = _regime_bars(12, 3200)
    B[-400:] = [list(r) for r in A[-400:]]           # the newest 400 bars equal, the older history different
    w = se.signal_window(b.packs, b.ind_settings)
    cache = {}
    se.pack_signals(A, b.packs, b.ind_settings, 0.0, 2800, cache=cache, salt=3)
    got = se.pack_signals(B, b.packs, b.ind_settings, 0.0, 2800, cache=cache, salt=3)["trend"]
    want = se.pack_signals(B, b.packs, b.ind_settings, 0.0, 2800)["trend"]
    differ = sum(1 for x, y in zip(se.pack_signals(A, b.packs, b.ind_settings, 0.0, 2800)["trend"][2800:], want[2800:]) if x != y)
    ok = w >= 1440 and got == want and differ > 0
    return ok, f"signal window {w}; memo result equals a fresh pass={got == want}; bars where the older history changes the trend signal {differ}"


CHECKS = [
    ("indication.match-follows-timeframe-lane", match_follows_the_timeframe_lane_of_the_pick),
    ("indication.missing-pnl-pct-is-not-a-loss", a_close_without_pnl_pct_is_not_a_loss_in_exits_and_dca),
    ("indication.bars-to-entry-reason-round-trip", bars_to_entry_reason_round_trip),
    ("indication.extra-book-fetches-once-in-flight", extra_book_fetches_a_key_once_while_in_flight),
    ("indication.causal-signals-ignore-future", causal_signals_ignore_the_future),
    ("indication.library-memo-never-stale", library_memo_never_serves_another_state_of_the_rows),
    ("indication.every-name-gets-its-timeframe-history", every_name_gets_the_history_its_timeframe_needs),
    ("indication.signal-memo-key-covers-the-window", the_signal_memo_key_covers_every_bar_a_pack_reads),
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
