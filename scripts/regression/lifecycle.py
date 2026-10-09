"""Lifecycle suite: every configured Set is an independent unit, evaluated on an interval, valid or invalid on its own tape.

Covers: one state per configured Set and counts that sum to the grid (1174 with TP ignored, 936 with TP honoured);
interval-driven refresh, never back to back; every Set evaluated on a full pass; a failing Set isolated while the
others are scored; an invalid Set stays in processing and becomes valid again with no reset; a lock blocks at once
and is undone only by unlocking; entries per Set through valid_for (no global gate); freshness in live; the one rule
holds for every valid Set after a replay; retired config keys are gone; the trader reads only attributes that exist.
"""
from __future__ import annotations

import json
import os
import re
import time

from common import PULSE, T0, overlay, synth_bars  # noqa: F401
from set_engine import SetBook

RETIRED_KEYS = ("setAutoDeact", "setReactivate", "setMaxActive")
GATE = dict(setGateWindow=15, setGateMinTrades=12, setMinPf=1.10)


def _book(**extra):
    b = SetBook()
    b.load(overlay(**extra))
    b.cost_pct = 0.15
    return b


def _bars_book(n_syms=2, bars=500, seed=11, **extra):
    b = _book(**extra)
    for i in range(n_syms):
        b.bars[f"L{i}-USDT"] = synth_bars(seed + i, bars, drift=0.0004 if i % 2 == 0 else -0.0004)
    return b


def _rows(n, win_net=0.001, loss_net=-0.001, loss_every=4, t0=6000):
    """Closed rows with a net result each. The gate takes the cost once: pnl_pct = net + 0.15%."""
    out = []
    for i in range(n):
        net = loss_net if i % loss_every == 0 else win_net
        out.append({"t": t0 + i * 60, "pnl": net, "pnl_pct": net + 0.0015, "symbol": "T", "side": "LONG",
                    "hold_s": 60, "reason": "sl" if net < 0 else "tp", "set_id": ""})
    return out


def coverage_counts_sum_to_grid():
    ig = _book(exitIgnoreTp=True)
    hn = _book(exitIgnoreTp=False)
    ci, ch = ig.counts(), hn.counts()
    ok = (len(ig.by_idx) == 1174 and len(hn.by_idx) == 936
          and ci["sum"] == ci["configured"] == 1174 and ch["sum"] == ch["configured"] == 936
          and ci["total"] == 1174 + len(ig.retired) and ch["total"] == 936 + len(hn.retired))
    return ok, f"ignore configured={ci['configured']} sum={ci['sum']} | honour configured={ch['configured']} sum={ch['sum']}"


def interval_never_back_to_back():
    b = _book()
    now = time.time()
    b.progress.ready = False
    b.last_run = now
    right_after_a_pass = b.due()
    b.last_run = now - b.retry_s - 1
    retry_fires = b.due()
    b.progress.ready = True
    b.last_run = now - b.refresh_s / 2
    mid_interval = b.due()
    b.last_run = now - b.refresh_s - 1
    refresh_fires = b.due()
    ok = (not right_after_a_pass) and retry_fires and (not mid_interval) and refresh_fires
    return ok, f"right_after={right_after_a_pass} retry={retry_fires} mid={mid_interval} refresh={refresh_fires}"


def every_set_evaluated_and_valid_passes_gates():
    # a looser PF bar on this synthetic market gives hundreds of valid Sets, so the invariant is not vacuous
    b = _bars_book(n_syms=2, bars=500, setMinPf=0.9, setGateMinTrades=5)
    b.replay_all(now=T0)
    unevaluated = [s.id for s in b.by_idx if s.evaluated_at <= 0]
    thr = b.max_dd_s
    violations = [s.id for s in b.by_idx if b.is_eligible(s) and not (
        s.gate_n >= b.gate_min and s.gate_pf + 1e-9 >= b.min_pf and s.max_dd_s < thr)]
    valid = sum(1 for s in b.by_idx if b.is_eligible(s))
    ok = not unevaluated and not violations and valid > 0
    return ok, f"configured={len(b.by_idx)} unevaluated={len(unevaluated)} valid={valid} gate_violations={len(violations)}"


def failing_set_isolated_others_score():
    b = _bars_book()
    bad = b.by_idx[0].id
    orig = b._score_one

    def flaky(st, now=None):
        if st.id == bad:
            raise RuntimeError("injected scoring fault")
        return orig(st, now=now)

    b._score_one = flaky
    b.replay_all(now=T0)
    st_bad = b.sets[bad]
    others = [s for s in b.by_idx if s.id != bad]
    scored = sum(1 for s in others if s.evaluated_at > 0)
    state = b.state_of(st_bad)
    ok = bool(st_bad.last_error) and state == ("error", "error") and scored == len(others) and b.progress.errors >= 1
    return ok, f"bad={state} scored_others={scored}/{len(others)} errors={b.progress.errors}"


def invalid_set_recovers_without_reset():
    b = _book(**GATE)
    st = next(s for s in b.by_idx if s.kind == "base")
    st.hist = _rows(15, loss_every=1)           # every order a loss: PF 0
    b._score_one(st)
    off = b.state_of(st)
    st.hist = _rows(15)                         # PF 2.75 on the next evaluation, nothing else touched
    b._score_one(st)
    on = b.state_of(st)
    ok = off == ("invalid", "pf<min") and on == ("valid", "")
    return ok, f"off={off} on={on}"


def lock_blocks_at_once_and_unlock_restores():
    b = _book(**GATE)
    st = next(s for s in b.by_idx if s.kind == "base")
    st.hist = _rows(15)
    b._score_one(st)
    was = b.state_of(st)
    st.locked = True                            # blocks at once, before any replay
    locked = b.state_of(st)
    blocked = not b.is_eligible(st)
    st.locked = False
    b._score_one(st)
    back = b.state_of(st)
    ok = was == ("valid", "") and locked == ("locked", "lock") and blocked and back == ("valid", "")
    return ok, f"was={was} locked={locked} back={back}"


def entries_per_set_no_global_gate():
    b = _book(**GATE)
    pack = b.packs[0]
    base = [s for s in b.by_idx if s.kind == "base" and s.pack == pack][:4]
    for s in base:
        s.hist = _rows(15)
        b._score_one(s)
    b.progress.ready = False                    # a pass in progress must not stop valid Sets from being picked
    all_valid = len(b.valid_for(pack, "base")) == len(base) and b.pack_open(pack)
    base[0].hist = _rows(15, loss_every=1)
    b._score_one(base[0])
    rest = b.valid_for(pack, "base")
    excluded = base[0] not in rest and len(rest) == len(base) - 1
    ranked = all(rest[i].gate_pf >= rest[i + 1].gate_pf for i in range(len(rest) - 1))
    picked_is_valid = b.pick_any(pack) in rest
    ok = all_valid and excluded and ranked and picked_is_valid
    return ok, f"all_valid={all_valid} excluded={excluded} ranked={ranked} picked_valid={picked_is_valid}"


def stale_set_not_valid_in_live():
    b = _book(**GATE)
    b.fresh_s = 180.0
    a, c = [s for s in b.by_idx if s.kind == "base"][:2]
    for s in (a, c):
        s.hist = _rows(15)
        b._score_one(s)
    a.evaluated_at = time.time() - 400          # not evaluated for longer than fresh_s
    stale = b.state_of(a)
    fresh = b.state_of(c)
    b._score_one(a)
    again = b.state_of(a)
    ok = stale == ("invalid", "stale") and fresh == ("valid", "") and again == ("valid", "")
    return ok, f"stale={stale} fresh={fresh} after_rescore={again}"


def counts_match_states_and_reasons():
    b = _bars_book(**GATE)
    sets = [s for s in b.by_idx if s.kind == "base"][:6]
    sets[0].hist = _rows(15)
    sets[1].hist = _rows(15, loss_every=1)
    sets[2].locked = True
    sets[3].last_error = "injected"
    sets[4].hist = _rows(5)                     # below the order floor
    for s in sets:
        b._score_one(s)
    c = b.counts()
    ok = (c["sum"] == c["configured"] == len(b.by_idx) and c["valid"] >= 1 and c["locked"] >= 1 and c["error"] >= 1
          and sum(c["reasons"].values()) == c["invalid"] and set(c["reasons"]) <= {"n<min", "pf<min", "ddt", "stale", "inactive"}
          and c["reasons"].get("n<min", 0) >= 1 and c["reasons"].get("pf<min", 0) >= 1)
    return ok, f"valid={c['valid']} invalid={c['invalid']} locked={c['locked']} error={c['error']} reasons={c['reasons']}"


def snapshot_counts_match_book():
    b = _bars_book()
    b.replay_all(now=T0)
    snap = b.snapshot()
    c = snap["counts"]
    rows_ok = all("state" in r and "stateReason" in r and "evaluatedAt" in r for r in snap["rows"])
    ok = c == b.counts() and c["sum"] == c["configured"] and rows_ok and snap["freshS"] == b.fresh_s
    return ok, f"sum={c['sum']} configured={c['configured']} rows_have_state={rows_ok}"


def retired_config_keys_gone():
    leftover = {}
    for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
        ov = json.load(open(os.path.join(PULSE, name)))
        leftover[name] = [k for k in RETIRED_KEYS if k in ov]
    b = _book(setMaxActive=5, setReactivate=False, setAutoDeact=False)
    attrs = [a for a in ("auto_deact", "reactivate", "max_active", "_cap_active") if hasattr(b, a)]
    ok = not any(leftover.values()) and not attrs
    return ok, f"overlay keys left={leftover} book attrs={attrs}"


def trader_reads_existing_set_attributes():
    """Static cross-check: every self.sets.<name> the trader reads must exist on a loaded SetBook.

    A removed attribute left in the status payload raises at runtime; this catches it before deploy."""
    src = open(os.path.join(PULSE, "pulse_trader.py")).read()
    names = sorted(set(re.findall(r"self\.sets\.([A-Za-z_]\w*)", src)))
    b = _book()
    b.fresh_s = 0.0
    missing = [n for n in names if not hasattr(b, n)]
    return not missing, f"{len(names)} attributes read by the trader, missing={missing}"


CHECKS = [
    ("lifecycle.coverage-counts-sum-to-grid", coverage_counts_sum_to_grid),
    ("lifecycle.interval-never-back-to-back", interval_never_back_to_back),
    ("lifecycle.every-set-evaluated-valid-passes-gate", every_set_evaluated_and_valid_passes_gates),
    ("lifecycle.failing-set-isolated", failing_set_isolated_others_score),
    ("lifecycle.invalid-recovers-without-reset", invalid_set_recovers_without_reset),
    ("lifecycle.lock-blocks-at-once", lock_blocks_at_once_and_unlock_restores),
    ("lifecycle.entries-per-set-no-global-gate", entries_per_set_no_global_gate),
    ("lifecycle.stale-set-not-valid-live", stale_set_not_valid_in_live),
    ("lifecycle.counts-match-states-and-reasons", counts_match_states_and_reasons),
    ("lifecycle.snapshot-counts-match-book", snapshot_counts_match_book),
    ("lifecycle.retired-config-keys-gone", retired_config_keys_gone),
    ("lifecycle.trader-reads-existing-attributes", trader_reads_existing_set_attributes),
]


if __name__ == "__main__":
    bad = 0
    for name, fn in CHECKS:
        ok, detail = fn()
        bad += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + name, detail)
    raise SystemExit(1 if bad else 0)
