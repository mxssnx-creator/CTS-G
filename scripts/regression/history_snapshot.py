"""Opt-in history snapshots: no mixed replay, stale evidence, or lost live-close gates."""
from __future__ import annotations

import time
import threading

from common import T0, synth_bars
from set_engine import SetBook

NAMES = ("A-USDT", "B-USDT")


def _book(opt_in=True, live_clock=False, stages=False):
    book = SetBook()
    book.load({"histKeepLastComplete": opt_in, "histLookbackBars": 240, "histLookbackMax": 480,
               "histLookbackAuto": True, "histLookbackTargetN": 20, "histMinBars": 60,
               "histWarmup": 16, "holdEnabled": False, "setStepAdapt": False,
               "setMinStep": 2, "setStepMax": 8, "setSlRatios": [0.5, 1.0],
               "trailVariants": ["0.3:0.1"], "stratGeneral": True, "stratIndications": False,
               "stagesEnabled": stages, "setGateWindow": 20, "setGateMinTrades": 1,
               "setGateShortN": 3, "setGateMinN": 0, "mainGateEnabled": True,
               "mainGateN": 10, "mainShortEnabled": True, "mainShortWindows": [3, 5],
               "mainShortMin": 3, "mainMinN": 0,
               "setMinPf": 1.1, "minPfBySize": False, "positionCostPct": 0.12,
               "realGateN": 1})
    book.bars = {s: synth_bars(i + 1, 120) for i, s in enumerate(NAMES)}
    if live_clock:
        book.fresh_s = 180.0
        book.bar_t = {s: T0 - 60 for s in NAMES}

    def replay(symbol, histories, now, on_step=None):
        # Old complete window: two +0.28%-net closes per symbol and Set.
        # Grown window: two -0.52%-net closes; a partial blend must not score.
        gross = 0.004 if book.lookback == 240 else -0.004
        for st in book.by_idx:
            offsets = (780, 660, 540, 420, 300, 180) if stages else (180, 120)
            histories[st.id].extend({"t": T0 - offset, "symbol": symbol, "side": "LONG",
                                     "pnl": gross - 0.0012, "pnl_pct": gross, "hold_s": 60,
                                     "reason": "fixture", "set_id": st.id}
                                    for offset in offsets)
        return {}

    book._replay_symbol = replay
    for symbol in NAMES:
        book.replay_all(now=T0, symbols=[symbol], expected_symbols=NAMES)
    return book


def default_still_blocks():
    b = _book(opt_in=False)
    st = b.by_idx[0]
    return (b._lookback_pending and b.base_validation_reason(st) == "history"
            and not b.hist_keep_last_complete), f"pending={b._lookback_pending}, gate={b.base_validation_reason(st)}"


def complete_snapshot_and_atomic_promotion():
    b = _book()
    st = b.by_idx[0]
    before = (st.gate_n, st.gate_pf, st.evaluated_at, list(st.hist), b.gate_reason(st))
    b.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=NAMES)
    middle = (st.gate_n, st.gate_pf, st.evaluated_at, list(st.hist), b.gate_reason(st))
    promotion_reasons = []
    score = b._score_one
    def check_promotion(item, now=None):
        promotion_reasons.append(b.base_validation_reason(item))
        return score(item, now=now)
    b._score_one = check_promotion
    b.replay_all(now=T0, symbols=[NAMES[1]], expected_symbols=NAMES)
    after = (st.gate_n, st.gate_pf, b.gate_reason(st), b._lookback_pending)
    ok = (before == middle and before[0] == 4 and before[1] == 99.0 and before[-1] == ""
          and b._history_snapshot_lookback == 480 and after == (4, 0.0, "pf<min", False)
          and len(promotion_reasons) == len(b.by_idx) and set(promotion_reasons) == {"history"})
    return ok, f"old={(before[0], before[1], before[-1])}, unchanged={before == middle}, new={after}, promotion={set(promotion_reasons)}"


def live_close_restricts_only_its_own_set():
    b = _book()
    losing, other = b.by_idx[:2]
    old_evaluation = losing.evaluated_at
    b.on_live_close({"ours": True, "set_id": losing.id, "t": T0 + 10, "symbol": NAMES[0],
                     "side": "LONG", "pnl_pct": -0.004, "net_pnl_pct": -0.005,
                     "fee_source": "reported-fee", "client_id": "fixture-close", "lane": "pos"})
    stats = b.execution_stats(losing, "pos")
    result = (b.gate_reason(losing), b.gate_reason(other), stats["n"], stats["gl"],
              losing.evaluated_at == old_evaluation)
    replay = b._replay_symbol
    def winners(symbol, histories, now, on_step=None):
        data = replay(symbol, histories, now, on_step=on_step)
        for rows in histories.values():
            for row in rows:
                row["pnl_pct"], row["pnl"] = 0.004, 0.0028
        return data
    b._replay_symbol = winners
    for symbol in NAMES:
        b.replay_all(now=T0, symbols=[symbol], expected_symbols=NAMES)
    promoted = (b._lookback_pending, b.gate_reason(losing), b.gate_reason(other), b.execution_stats(losing, "pos")["n"])
    ok = (result[:3] == ("execution", "", 1) and abs(result[3] - 0.005) < 1e-12 and result[4]
          and promoted == (False, "execution", "", 1))
    return ok, f"during={result}, after promotion={promoted}"


def stale_is_per_set_and_oldest_source():
    b = _book()
    first, second = b.by_idx[:2]
    first.evaluated_at = time.time() - b._snapshot_max_age() - 1
    per_set = (b.gate_reason(first), b.gate_reason(second))
    b._history_snapshot_oldest_at = time.time() - b._snapshot_max_age() - 1
    source = b.gate_reason(second)
    return per_set == ("stale", "") and source == "stale", f"sets={per_set}, oldest source={source}"


def missing_universe_and_bad_bars_fail_closed():
    missing = _book()
    missing.bars.pop(NAMES[1])
    missing.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=NAMES)
    m = (missing._lookback_pending, missing._history_snapshot_active, missing.gate_reason(missing.by_idx[0]),
         missing._lookback_retry_fast)
    gap = _book()
    gap.bars.pop(NAMES[0])  # a timestamp-gap refresh discarded the old series
    gap.ingest_bars(NAMES[0], synth_bars(19, 120), last_t=T0)
    g = gap.gate_reason(gap.by_idx[0])
    quality = _book()
    quality.ingest_bars(NAMES[0], [[1.0, 1.0, 1.0, 1.0, 0.0]] * 120, last_t=T0)
    q = quality.gate_reason(quality.by_idx[0])
    return m == (True, False, "history", False) and g == q == "history", f"missing={m}, gap={g}, bad bars={q}"


def failed_or_aborted_rebuild_keeps_old_score():
    b = _book()
    st = b.by_idx[0]
    before = (st.gate_pf, st.evaluated_at, list(st.hist))
    replay = b._replay_symbol
    def failed(*args, **kwargs):
        raise ValueError("fixture history page unavailable")
    b._replay_symbol = failed
    b.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=NAMES)
    failure = (b._lookback_pending, b.gate_reason(st), b.progress.errors, b._lookback_retry_fast)
    b._replay_symbol = replay
    b.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=NAMES, abort=lambda: True)
    aborted = (b._lookback_pending, b.gate_reason(st), b.progress.phase, b._lookback_retry_fast)
    after = (st.gate_pf, st.evaluated_at, list(st.hist))
    return (failure == (True, "", 1, False) and aborted == (True, "", "partial", False) and before == after), f"failure={failure}, abort={aborted}, unchanged={before == after}"


def stale_or_undated_components_cannot_be_promoted():
    b = _book(live_clock=True)
    b.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=NAMES)
    b._hist_replay_at[NAMES[0]] = time.time() - b._snapshot_max_age() - 1
    b.replay_all(now=T0, symbols=[NAMES[1]], expected_symbols=NAMES)
    old_replay = (b._lookback_pending, b.by_idx[0].gate_pf)
    b._hist_replay_at[NAMES[0]] = time.time()
    b._hist_replay_bar_at[NAMES[0]] = T0 - b._snapshot_max_age() - 120
    b.replay_all(now=T0, symbols=[NAMES[1]], expected_symbols=NAMES)
    old_bar = (b._lookback_pending, b.by_idx[0].gate_pf)
    b._hist_replay_bar_at[NAMES[0]] = None
    b.replay_all(now=T0, symbols=[NAMES[1]], expected_symbols=NAMES)
    undated = (b._lookback_pending, b.by_idx[0].gate_pf)
    return old_replay == old_bar == undated == (True, 99.0), f"old replay={old_replay}, old bar={old_bar}, undated={undated}"


def topology_change_invalidates_snapshot():
    b = _book()
    b._rebuild_sets()
    reason = b.gate_reason(b.by_idx[0])
    return not b._history_snapshot_active and reason == "history", f"active={b._history_snapshot_active}, gate={reason}"


def snapshot_horizon_is_used_by_downstream_stages():
    b = _book()
    st = b.by_idx[0]
    b.by_idx = [st]
    st.score_at = T0
    st.proc_replayed = True
    st.proc = [(T0 - 300 * 60, 0.02), (T0 - 10 * 60, 0.03)]
    st.exe = [(T0 - 300 * 60, 0.04), (T0 - 10 * 60, 0.05)]
    st.neg = []
    main, real, _, _ = b._streams(st)
    scope = b.real_paper_scopes()
    params = b.stage_params(st.pack)
    ok = (main == [0.03] and real == [0.05] and params.lookback_s == 240 * 60
          and len(scope["overall"]) == 1 and abs(scope["overall"][0]["pnl"] - 0.05) < 1e-12)
    return ok, f"main={main}, real={real}, lookback={params.lookback_s}, scoped={len(scope['overall'])}"


def bar_invalidation_is_atomic_with_selection():
    b = _book()
    inside, release, done = threading.Event(), threading.Event(), threading.Event()
    usable = b.usable
    def hold_quality(symbol):
        inside.set()
        if not release.wait(2):
            raise RuntimeError("fixture release timed out")
        return usable(symbol)
    b.usable = hold_quality
    selected = []
    writer = threading.Thread(target=lambda: b.ingest_bars(NAMES[0], [[1.0, 1.0, 1.0, 1.0, 0.0]] * 120, last_t=T0))
    def choose():
        selected.append(b.pick_any("general"))
        done.set()
    reader = threading.Thread(target=choose)
    writer.start()
    entered = inside.wait(2)
    reader.start()
    blocked = not done.wait(0.1)
    release.set()
    writer.join(2)
    reader.join(2)
    ok = entered and blocked and done.is_set() and selected == [None] and not writer.is_alive() and not reader.is_alive()
    return ok, f"entered={entered}, selection waited={blocked}, result={selected}, completed={done.is_set()}"


def live_snapshot_accounts_for_bar_age():
    b = _book(live_clock=True)
    # The replay completed recently, but its oldest completed candle was
    # already two minutes old at the decision time.
    b._lookback_pending = False
    b._history_snapshot_active = False
    b.lookback = 240
    b._lookback_grown = 0
    b._hist_replay_key.clear()
    b._hist_replay_at.clear()
    b.bar_t = {s: T0 - 180 for s in NAMES}
    for s in NAMES:
        b.replay_all(now=T0, symbols=[s], expected_symbols=NAMES)
    age = time.time() - b._history_snapshot_oldest_at
    before = b.gate_reason(b.by_idx[0])
    b.fresh_s = 90.0
    after = b.gate_reason(b.by_idx[0])
    return b._lookback_pending and 119 <= age < 125 and before == "" and after == "stale", f"source age={age:.1f}s, before={before!r}, after={after!r}"


def staged_lanes_retain_then_recalculate_their_own_evidence():
    b = _book(stages=True)
    st = b.by_idx[0]
    old = (st.gate_n, st.gate_pf, dict(st.stage), list(st.proc), list(st.exe), b.gate_reason(st))
    b.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=NAMES)
    middle = (st.gate_n, st.gate_pf, dict(st.stage), list(st.proc), list(st.exe), b.gate_reason(st))
    b.replay_all(now=T0, symbols=[NAMES[1]], expected_symbols=NAMES)
    after = (st.gate_n, st.gate_pf, b.gate_reason(st))
    ok = (old == middle and old[0] == 12 and old[1] == 99.0 and old[-1] == ""
          and old[2].get("pos") and after == (12, 0.0, "pf<min"))
    return ok, f"old n/pf/stage/gate={(old[0], old[1], old[2], old[-1])}, retained={old == middle}, new={after}"


def expected_universe_excludes_unexpected_tapes():
    b = _book()
    b._lookback_pending = False
    b._history_snapshot_active = False
    b.lookback = 240
    b._lookback_grown = 0
    b.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=[NAMES[0]])
    result = (b.by_idx[0].n, b._history_snapshot_symbols, b._lookback_pending)
    return result == (2, frozenset([NAMES[0]]), True), f"n/symbols/pending={result}"


def evidence_expiring_during_scoring_cannot_promote():
    b = _book()
    b.replay_all(now=T0, symbols=[NAMES[0]], expected_symbols=NAMES)
    score = b._score_one
    calls = []
    def expires(item, now=None):
        if not calls:
            b._hist_replay_at[NAMES[0]] -= b._snapshot_max_age() + 1
        calls.append(item.id)
        return score(item, now=now)
    b._score_one = expires
    b.replay_all(now=T0, symbols=[NAMES[1]], expected_symbols=NAMES)
    result = (b._lookback_pending, b._history_snapshot_active, b.progress.phase,
              b.base_validation_reason(b.by_idx[0]))
    return result == (True, False, "partial", "history") and bool(calls), f"after scoring={result}, scored={len(calls)}"


CHECKS = [
    ("history_snapshot.default-blocks", default_still_blocks),
    ("history_snapshot.atomic-promotion", complete_snapshot_and_atomic_promotion),
    ("history_snapshot.live-close-isolated", live_close_restricts_only_its_own_set),
    ("history_snapshot.oldest-source-and-set-freshness", stale_is_per_set_and_oldest_source),
    ("history_snapshot.missing-or-invalid-universe", missing_universe_and_bad_bars_fail_closed),
    ("history_snapshot.failure-and-abort", failed_or_aborted_rebuild_keeps_old_score),
    ("history_snapshot.reject-old-or-undated-components", stale_or_undated_components_cannot_be_promoted),
    ("history_snapshot.topology-change", topology_change_invalidates_snapshot),
    ("history_snapshot.stage-horizon", snapshot_horizon_is_used_by_downstream_stages),
    ("history_snapshot.atomic-bar-invalidation", bar_invalidation_is_atomic_with_selection),
    ("history_snapshot.live-bar-age", live_snapshot_accounts_for_bar_age),
    ("history_snapshot.staged-lanes", staged_lanes_retain_then_recalculate_their_own_evidence),
    ("history_snapshot.expected-universe-only", expected_universe_excludes_unexpected_tapes),
    ("history_snapshot.expiry-during-promotion", evidence_expiring_during_scoring_cannot_promote),
]
