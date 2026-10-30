"""The stage chain (Base -> Main -> Real) as a whole: every last-N window on its own, causal, neutral, costed once, debuggable.

These checks stand next to the engine's own self-tests (set_engine.self_test) and read the chain from outside:
  - each last-N window is judged on its own and the stage is their conjunction (changing another window's setting never moves it);
  - the per-window figures the stats report are the pooled figures of exactly the orders that window reads;
  - the chain is causal: an order's place in the Main / Real Set depends only on results that closed before its entry;
  - the chain is neutral: on results without persistence it adds no PF and removes none (the "drop" after a stage is the selection gap), and on results
    with persistence it lifts the PF of what follows (so a defect that destroyed persistence would show);
  - a Set's verdict ignores every other Set's tape;
  - costs: a net result is the gross move minus one PositionCost, stops and targets sit at the lane's distances, a live close and a replay row of the same
    gross move land on the same net;
  - a plain reading of the rules (scripts/main_stage_audit.naive_chain) and the process debug (scripts/stage_debug) agree with the engine's chain.
"""
from __future__ import annotations

import bisect
import os
import random
import sys
from dataclasses import replace

from common import COST, ROOT, SMALL, T0, overlay, synth_bars  # noqa: F401
import set_engine as se
from set_engine import SetBook, StageParams, stage_chain, stage_verdicts

sys.path.insert(0, os.path.join(ROOT, "scripts"))

CFG_FULL = StageParams(base_long=20, base_short=8, gate_min=1, floor=1.25, max_dd_s=1e12, lookback_s=1e12, consistency=0.0, main_on=True, main_n=8, short_on=True,
                       short_windows=(4,), short_min=3, short_margin=1.0, neg_on=True, neg_windows=(2, 4), real_on=True, real_n=4, full_window=False)


def _tape(rng, n, kind="mixed", gap=60.0, hold=300.0, irregular=False):
    """Net results with exit times and entry times. kind: iid (no persistence), regime (results come in streaks), mixed. irregular: random gaps (mean `gap`)."""
    if kind == "iid":
        nets = [rng.choice((0.006, 0.004, -0.004, -0.005, -0.006)) for _ in range(n)]
    elif kind == "regime":
        nets, good = [], True
        for _ in range(n):
            if rng.random() < 0.04:
                good = not good
            p = 0.7 if good else 0.25
            nets.append(0.005 if rng.random() < p else -0.005)
    else:
        nets = [rng.choice((0.006, 0.005, -0.004, -0.005)) for _ in range(n)]
    if irregular:
        ts, t = [], 1000.0
        for _ in range(n):
            t += rng.expovariate(1.0 / gap)
            ts.append(t)
    else:
        ts = [1000.0 + gap * k for k in range(n)]
    ents = [t - hold for t in ts]
    return ts, ents, nets


def _configs():
    """A spread of window configurations, each a StageParams: the three Base windows and the three Main windows on and off, both modes, both window rules."""
    out = {"shipped-like": CFG_FULL}
    for full in (False, True):
        out[f"full={full}"] = replace(CFG_FULL, full_window=full)
    for n in (1, 2, 3):
        for pos in (True, False):
            out[f"base minimal {'+' if pos else '-'}{n}"] = replace(CFG_FULL, base_min_n=n, base_min_positive=pos)
            out[f"main minimal {'+' if pos else '-'}{n}"] = replace(CFG_FULL, main_min_n=n, main_min_positive=pos)
    for n in (1, 2, 3):
        out[f"base minimal both {n}"] = replace(CFG_FULL, base_min_n=n, base_min_both=True)
        out[f"main minimal both {n}"] = replace(CFG_FULL, main_min_n=n, main_min_both=True)
    out["both minimal"] = replace(CFG_FULL, base_min_n=2, base_min_positive=False, main_min_n=2, main_min_positive=True)
    out["no short windows"] = replace(CFG_FULL, base_short=0, short_on=False)
    out["main off"] = replace(CFG_FULL, main_on=False)
    out["negative off"] = replace(CFG_FULL, neg_on=False)
    out["consistency"] = replace(CFG_FULL, consistency=1.0)
    out["size-dependent PF floors"] = replace(CFG_FULL, size_floors=((4, 1.05), (8, 1.1), (20, 1.4)), long_floor=1.7)
    out["lookback 4000 s"] = replace(CFG_FULL, lookback_s=4000.0)
    out["drawdown limit 900 s"] = replace(CFG_FULL, max_dd_s=900.0, lookback_s=1e12)
    return out


def the_plain_reading_equals_the_chain():
    """scripts/main_stage_audit.naive_chain (sums in loops, no prefix sums, no shortcuts) and set_engine.stage_chain give the same Main, Real, Real Negative
    Sets and the same Main short window under every window configuration, on streaky, i.i.d. and mixed tapes with regular and irregular spacing (the lookback and drawdown-limit configurations bind on the irregular ones)."""
    import main_stage_audit as audit
    rng = random.Random(21)
    bad, n_cfg, n_tapes, orders = [], 0, 0, 0
    for label, P in _configs().items():
        n_cfg += 1
        for kind, gap, irr in (("regime", 60.0, False), ("iid", 600.0, False), ("mixed", 30.0, False), ("mixed", 200.0, True)):
            ts, en, nets = _tape(rng, 160, kind, gap, hold=rng.choice((120.0, 600.0)), irregular=irr)
            n_tapes += 1
            orders += len(ts)
            if stage_chain(ts, en, nets, P) != audit.naive_chain(ts, en, nets, P):
                bad.append((label, kind))
    return not bad, f"{n_cfg} configurations x 4 tapes ({orders} orders): chains differing from the plain reading: {bad[:4] or 0}"


def the_debug_trace_equals_the_engine():
    """scripts/stage_debug.trace_set follows the chain order by order: its flags rebuild the engine's lists exactly, the stop reasons are the stage rules'
    (orders / long / short / minimal at Base, main / mainshort / mainminimal at Main), and the verdict it prints is stage_verdicts of the same lists."""
    import stage_debug as dbg
    rng = random.Random(33)
    bad, reasons_seen, n = [], set(), 0
    for label, P in _configs().items():
        ts, en, nets = _tape(rng, 140, rng.choice(("regime", "mixed")), 60.0)
        tr = dbg.trace_set(ts, en, nets, P)
        ch = stage_chain(ts, en, nets, P)
        v = stage_verdicts([nets[k] for k in ch["main"]], [nets[k] for k in ch["real"]], [nets[k] for k in ch["realneg"]], ch["short_n"], P)
        n += 1
        ok = dbg.consistent(tr) and tr["main"] == ch["main"] and tr["real"] == ch["real"] and tr["realneg"] == ch["realneg"] and tr["verdict"] == v
        for o in tr["orders"]:
            reasons_seen.add(o["base_stop"] or o.get("main_stop") or "in Real Set")
        if not ok:
            bad.append(label)
        text = dbg.render(tr, 5)
        if "Main Set" not in text or "verdict now" not in text:
            bad.append(label + " (render)")
    want = {"orders", "long", "short", "minimal", "main", "mainshort", "mainminimal", "in Real Set"}
    return not bad and want <= reasons_seen, f"{n} configurations traced; mismatches {bad[:4] or 0}; stop reasons seen {sorted(reasons_seen)}"


def the_chain_is_causal():
    """An order's place in the Main, Real and Real Negative Sets depends only on results that closed before its entry: with every result that closes at or after
    a cut time replaced by noise, every order that was entered before the cut keeps its place, under every window configuration."""
    rng = random.Random(55)
    bad, n = [], 0
    for label, P in _configs().items():
        ts, en, nets = _tape(rng, 200, "mixed", 60.0, hold=400.0)
        base = stage_chain(ts, en, nets, P)
        cut = ts[rng.randint(40, 150)]
        alt = [rng.choice((0.02, -0.02, 0.0)) if t >= cut else x for t, x in zip(ts, nets)]
        other = stage_chain(ts, en, alt, P)
        n += 1
        for name in ("main", "real", "realneg"):
            a = [k for k in base[name] if en[k] < cut]
            b = [k for k in other[name] if en[k] < cut]
            if a != b:
                bad.append((label, name))
    return not bad, f"{n} configurations: orders entered before the cut moved when later results changed: {bad[:4] or 0}"


def the_chain_is_neutral_without_persistence_and_lifts_it_with():
    """The selection gap, measured on tapes. Results without persistence (i.i.d. per Set): the pooled PF of the Main Set (orders opened while Base passed) equals
    the PF of the whole tapes within noise while the PF at selection is far above it - a stage cannot add or remove PF from nothing. Results in streaks (a regime
    persists): the Main Set's PF rises above the tape's. A defect that made later orders worse (or better) than their selection implies would break the first
    or the second."""
    rng = random.Random(77)
    out = {}
    for kind in ("iid", "regime"):
        g_all = l_all = g_main = l_main = sel_g = sel_l = 0.0
        for _ in range(240):
            ts, en, nets = _tape(rng, 600, kind, 60.0, hold=200.0)
            P = replace(CFG_FULL, base_long=30, base_short=0, floor=1.25, main_on=False, neg_on=False, real_on=False)
            ch = stage_chain(ts, en, nets, P)
            g_all += sum(x for x in nets if x > 0)
            l_all += -sum(x for x in nets if x < 0)
            gpre, lpre = se.prefix_sums(nets)
            for k in ch["main"]:
                x = nets[k]
                g_main += max(x, 0.0)
                l_main += max(-x, 0.0)
                e = bisect.bisect_left(ts, en[k])
                s0 = max(0, e - 30)
                sel_g += gpre[e] - gpre[s0]
                sel_l += lpre[e] - lpre[s0]
        out[kind] = (g_all / l_all, g_main / l_main, sel_g / sel_l)
    iid_all, iid_main, iid_sel = out["iid"]
    reg_all, reg_main, reg_sel = out["regime"]
    ok = abs(iid_main - iid_all) < 0.06 and iid_sel > iid_all + 0.3 and reg_main > reg_all + 0.15
    return ok, (f"i.i.d. tapes: whole tape PF {iid_all:.3f}, Main Set {iid_main:.3f} (selection PF {iid_sel:.3f}); streaky tapes: whole {reg_all:.3f}, "
                f"Main Set {reg_main:.3f} (selection {reg_sel:.3f})")


def a_negative_window_of_cancelling_results_is_not_negative():
    """A Negative window whose results add up to exactly zero is not below zero, in the verdict (a direct sum) and in the chain (a difference of prefix sums, whose
    rounding can leave -1e-17): two results that cancel never open the Negative lane, a window under the tolerance does, and the chain and the verdict agree."""
    P = replace(CFG_FULL, neg_windows=(2,), main_on=True, short_on=False, base_short=0, base_long=3, gate_min=1, floor=0.5, real_on=False)
    cancel = stage_verdicts([0.3, 0.1, 0.2, -0.2], [], [], 8, P)["neg"]                # window [0.2, -0.2] = 0.0 exactly
    cancel2 = stage_verdicts([0.5, 0.005, -0.005], [], [], 8, P)["neg"]
    below = stage_verdicts([0.5, 0.005, -0.006], [], [], 8, P)["neg"]
    # through the chain: orders whose Main-Set results make a prefix-sum difference of about -1e-17 for a cancelling window; every Negative order of the chain must
    # satisfy the direct rule (its window sums below zero by more than the tolerance)
    rng = random.Random(4)
    bad = 0
    for _ in range(300):
        n = 80
        vals = [round(rng.choice((0.1, 0.2, 0.3, 0.7, -0.1, -0.2, -0.3, -0.7)) * 0.01, 4) for _ in range(n)]
        ts = [1000.0 + 60.0 * k for k in range(n)]
        en = [t - 100.0 for t in ts]
        ch = stage_chain(ts, en, vals, replace(P, base_long=2, neg_windows=(2,)))
        mn = [vals[k] for k in ch["main"]]
        mt = [ts[k] for k in ch["main"]]
        for k in ch["realneg"]:
            e = sum(1 for t in mt if t < en[k])
            if e < 2 or sum(mn[e - 2:e]) >= -1e-12:
                bad += 1
    return (not cancel) and (not cancel2) and below and bad == 0, f"[0.2, -0.2] negative={cancel}; [0.005, -0.005] negative={cancel2}; [0.005, -0.006] negative={below}; chain orders whose window does not sum below zero: {bad}"


def replay_and_current_verdict_use_the_same_pf_rounding():
    """Values either side of a four-decimal PF boundary have the same verdict in live scoring and the historical chain."""
    from common import ROOT
    import os
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import main_stage_audit as audit
    import stage_eval as sg
    p = replace(CFG_FULL, base_long=10, base_short=0, main_n=2, short_on=False, neg_on=False, real_on=False,
                floor=0.1, size_floors=((2, 1.25), (10, 0.1)), long_floor=0.1)
    ts = [1000.0 + i * 60 for i in range(4)]
    entries = [t - 1 for t in ts]
    cases = []
    for target, passes in ((1.24996, True), (1.24994, False)):
        vals = [0.01, 0.005, -0.005 / target, 0.001]
        chain = stage_chain(ts, entries, vals, p)
        current = stage_verdicts(vals[1:3], [], [], 2, p)["main"]
        base_params = replace(p, base_short=2, main_on=False)
        base_chain = stage_chain(ts[1:], entries[1:], vals[1:], base_params)
        b = SetBook()
        b.load(overlay(setGateWindow=10, setGateShortN=2, setMinPf=1.25))
        e = {"w": vals[1:3], "cf": 0.0, "floor_short": 1.25}
        cases.append((current, 3 in chain["real"], 2 in base_chain["main"], sg._short_ok(b, e), chain == audit.naive_chain(ts, entries, vals, p), passes))
    return all(row[:4] == (row[5],) * 4 and row[4] for row in cases), f"(current Main, replay Main, replay Base short, evaluator Base short, independent match, expected): {cases}"


def _scored_book(min_pf=None, **extra):
    """A book of the small Set grid with a streaky tape of 140 orders on every Set (seeded per Set: the same tapes in every book of a check), scored once, so that
    Sets pass the Base windows, the Main windows and Real in numbers worth comparing. min_pf: the floor every stage asks (None: the overlay's)."""
    b = SetBook()
    b.load(overlay(**{"stagesEnabled": True, "setGateWindow": 20, "setGateShortN": 8, "setGateMinTrades": 0, "mainGateN": 8, "mainShortWindows": [4], "mainShortMin": 3,
                      "realGateN": 4, "setMaxDdtHours": 36, **SMALL, **extra}))
    if min_pf is not None:
        b.min_pf = min_pf
    cf = b.cost_pct / 100.0
    last = 0.0
    for i, st in enumerate(b.by_idx):
        ts, en, nets = _tape(random.Random(1000 + i), 140, "regime", 60.0, hold=300.0)
        st.hist = [{"t": t, "pnl": x, "pnl_pct": x + cf, "hold_s": 300.0, "reason": "tp", "symbol": "X", "side": "LONG", "set_id": st.id} for t, x in zip(ts, nets)]
        st.live = []
        st.active = True
        last = max(last, ts[-1])
    b._now_for_checks = last + 60.0
    for st in b.by_idx:
        b._score_one(st, now=last + 60.0)
    return b


def every_window_is_judged_on_its_own():
    """Changing the setting of one window never moves another window's verdict: the Base long and short windows, the Main long and short windows and the
    Main minimal window are compared between books that differ only in the other windows' settings (same bars, same Sets)."""
    base = _scored_book(min_pf=1.1)
    alt_base_min = _scored_book(min_pf=1.1, setGateMinN=2, setGateMinMode="negative")                 # Base minimal on
    alt_main_min = _scored_book(min_pf=1.1, mainMinN=2, mainMinMode="negative")                       # Main minimal on
    alt_short_off = _scored_book(min_pf=1.1, mainShortEnabled=False)                                  # Main short off
    alt_base_short = _scored_book(min_pf=1.1, setGateShortN=5)                                        # Base short moved
    alt_main_long = _scored_book(min_pf=1.1, mainGateN=12)                                            # Main long moved
    n_sets = len(base.by_idx)
    diffs = {"base long vs base minimal": 0, "base short vs base minimal": 0, "base long vs main windows": 0, "main long vs main minimal": 0,
             "main long vs main short off": 0, "main short vs main minimal": 0, "base long vs base short": 0}
    for i in range(n_sets):
        s0, a, b, c, d = base.by_idx[i], alt_base_min.by_idx[i], alt_main_min.by_idx[i], alt_short_off.by_idx[i], alt_base_short.by_idx[i]
        diffs["base long vs base minimal"] += s0.base_long_ok != a.base_long_ok
        diffs["base short vs base minimal"] += s0.base_short_ok != a.base_short_ok
        diffs["base long vs main windows"] += s0.base_long_ok != b.base_long_ok or s0.base_long_ok != c.base_long_ok
        diffs["main long vs main minimal"] += (s0.stage or {}).get("main") != (b.stage or {}).get("main") or s0.main_pf != b.main_pf
        diffs["main long vs main short off"] += (s0.stage or {}).get("main") != (c.stage or {}).get("main") or s0.main_pf != c.main_pf
        diffs["main short vs main minimal"] += (s0.stage or {}).get("short") != (b.stage or {}).get("short") or s0.main_short_win != b.main_short_win
        diffs["base long vs base short"] += s0.base_long_ok != d.base_long_ok
    pf_rows = [base.snapshot()["pfBase"]["windows"], alt_main_min.snapshot()["pfBase"]["windows"]]
    # the long windows' pass counts do not depend on the other windows either
    same_counts = (pf_rows[0]["baseLong"]["sets"] == alt_base_min.snapshot()["pfBase"]["windows"]["baseLong"]["sets"]
                   == alt_main_min.snapshot()["pfBase"]["windows"]["baseLong"]["sets"]
                   and pf_rows[0]["baseShort"]["sets"] == alt_base_min.snapshot()["pfBase"]["windows"]["baseShort"]["sets"]
                   and pf_rows[0]["mainLong"]["pf"] == pf_rows[1]["mainLong"]["pf"])
    # a window's setting does change that window: Base short 5 vs 8 and Main long 12 vs 8 move their own figures somewhere
    own = (any(x.base_short_ok != y.base_short_ok or x.base_short_pf != y.base_short_pf for x, y in zip(base.by_idx, alt_base_short.by_idx))
           and any(x.main_pf != y.main_pf or x.main_n != y.main_n for x, y in zip(base.by_idx, alt_main_long.by_idx)))
    ok = all(v == 0 for v in diffs.values()) and same_counts and own
    return ok, f"{n_sets} Sets; verdicts moved by another window's setting: { {k: v for k, v in diffs.items() if v} or 0}; pass counts equal={same_counts}; own setting moves own window={own}"


def window_figures_pool_exactly_the_orders_each_window_reads():
    """pfBase.windows against the Sets' own streams: for every window the Sets that pass it alone, the share of the stage's population, the orders behind the PF
    and the pooled PF are recomputed from the Main / Real streams and the Base tape, not from the stored fields."""
    from position_cost import cost_as_frac
    b = _scored_book(min_pf=1.1, mainMinN=1, mainMinMode="positive", setGateMinN=1, setGateMinMode="positive")
    w = b.snapshot()["pfBase"]["windows"]
    cf = cost_as_frac(b.cost_pct)

    def pool(vals):
        gp, gl = sum(x for x in vals if x > 0), -sum(x for x in vals if x < 0)
        return round(se.pf_of(gp, gl), 4)
    base_only = [x for x in b.by_idx if not b.base_validation_reason(x)]
    tapes = {x.id: [se.finite(r.get("pnl_pct")) - cf for r in sorted(x.tape(), key=lambda r: se.finite(r.get("t"))) if r.get("pnl_pct") is not None] for x in b.by_idx}
    floor = b.min_pf
    want = {}
    # Base long / short / minimal
    pass_long = [x for x in b.by_idx if len(tapes[x.id][-b.gate_window:]) >= b.gate_min and pool(tapes[x.id][-b.gate_window:]) + 1e-9 >= floor and x.max_dd_s < b.max_dd_s]
    pass_short = [x for x in b.by_idx if len(tapes[x.id][-b.base_short:]) >= 1 and pool(tapes[x.id][-b.base_short:]) + 1e-9 >= floor]
    pass_min = [x for x in b.by_idx if tapes[x.id][-b.base_min_n:] and all(v > 0 for v in tapes[x.id][-b.base_min_n:])]
    want["baseLong"] = (len(pass_long), sum(len(tapes[x.id][-b.gate_window:]) for x in pass_long), pool([v for x in pass_long for v in tapes[x.id][-b.gate_window:]]))
    want["baseShort"] = (len(pass_short), sum(len(tapes[x.id][-b.base_short:]) for x in pass_short), pool([v for x in pass_short for v in tapes[x.id][-b.base_short:]]))
    want["baseMinimal"] = (len(pass_min), sum(len(tapes[x.id][-b.base_min_n:]) for x in pass_min), None)
    # Main long / short / minimal on the Main Sets of the Base-passing Sets
    m = {x.id: b._streams(x)[0] for x in base_only}
    pm = [x for x in base_only if m[x.id][-b.main_n:] and pool(m[x.id][-b.main_n:]) + 1e-9 >= floor]
    ps = [x for x in base_only if m[x.id][-(x.main_short_win or b.main_n):] and pool(m[x.id][-(x.main_short_win or b.main_n):]) + 1e-9 >= floor]
    pk = [x for x in base_only if m[x.id][-b.main_min_n:] and all(v > 0 for v in m[x.id][-b.main_min_n:])]
    want["mainLong"] = (len(pm), sum(len(m[x.id][-b.main_n:]) for x in pm), pool([v for x in pm for v in m[x.id][-b.main_n:]]))
    want["mainShort"] = (len(ps), sum(len(m[x.id][-(x.main_short_win or b.main_n):]) for x in ps), pool([v for x in ps for v in m[x.id][-(x.main_short_win or b.main_n):]]))
    want["mainMinimal"] = (len(pk), sum(len(m[x.id][-b.main_min_n:]) for x in pk), None)
    got = {k: (w[k]["sets"], w[k]["orders"], w[k]["pf"]) for k in want}
    bad = {k: (got[k], want[k]) for k in want if got[k][0] != want[k][0] or got[k][1] != want[k][1] or (got[k][2] != want[k][2] if want[k][2] is None else got[k][2] is None or abs(got[k][2] - want[k][2]) > 1e-3)}
    pops = (w["baseLong"]["pop"] == len(b.by_idx) and w["mainLong"]["pop"] == len(base_only) and w["baseShort"]["pop"] == len(b.by_idx))
    shares = all(abs(w[k]["pct"] - (100.0 * w[k]["sets"] / w[k]["pop"] if w[k]["pop"] else 0.0)) < 0.011 for k in want)
    chosen = sum(w["mainShort"]["chosen"].values()) == len(base_only)
    ok = not bad and pops and shares and chosen and w["baseMinimal"]["mode"] == "positive" and w["mainMinimal"]["on"]
    return ok, f"{len(b.by_idx)} Sets, {len(base_only)} past Base; windows (Sets / orders / PF): { {k: got[k] for k in want} }; mismatches {bad or 0}; populations {pops}; shares {shares}; chosen short windows add up {chosen}"


def a_set_verdict_ignores_every_other_set():
    """A Set's verdict, windows and chain depend on its own tape only: with every other Set's tape replaced by noise, the Set's fields are identical."""
    b = _scored_book(min_pf=1.1, mainMinN=1)
    probe = [b.by_idx[0], b.by_idx[len(b.by_idx) // 2], b.by_idx[-1]]
    keys = ("gate_pf", "gate_n", "base_long_ok", "base_short_ok", "base_short_pf", "main_pf", "main_n", "main_short_win", "main_short_pf", "exe_pf", "neg_pf", "proc_total", "exe_total")
    before = [({k: getattr(st, k) for k in keys}, b.gate_reason(st), dict(st.stage)) for st in probe]
    rng = random.Random(3)
    for st in b.by_idx:
        if st in probe:
            continue
        cf = b.cost_pct / 100.0
        st.hist = [{"t": 1000.0 + 60.0 * i, "pnl": x, "pnl_pct": x + cf, "hold_s": 60.0, "reason": "tp", "symbol": "X", "side": "LONG"}
                   for i, x in enumerate(rng.choice((0.02, -0.02)) for _ in range(80))]
        st.proc_replayed = False
        b._score_one(st)
    after = [({k: getattr(st, k) for k in keys}, b.gate_reason(st), dict(st.stage)) for st in probe]
    return before == after, f"{len(probe)} probe Sets; fields equal after {len(b.by_idx) - len(probe)} other Sets were replaced by noise: {before == after}"


def costs_are_applied_once_and_lanes_sit_at_their_distances():
    """The replay's rows: net = gross move minus one PositionCost; a stop exit sits at -SL and a target exit at +TP of the Set's lane distances
    (set_engine.lane_fracs); the chain reads the net; a live close and a replay row of the same gross move land on the same net result."""
    from position_cost import cost_as_frac
    b = SetBook()
    b.load(overlay(**{"stagesEnabled": True, "setGateWindow": 20, "setMaxDdtHours": 36, **SMALL}))
    for i in range(4):
        b.bars[f"S{i}-USDT"] = synth_bars(11 + i, 1500, drift=0.0006 if i % 2 == 0 else -0.0006, vol=0.003)
    b.replay_all(now=T0)
    cf = cost_as_frac(b.cost_pct)
    n_rows = n_stop = n_tp = 0
    worst_net = worst_sl = worst_tp = 0.0
    for st in b.by_idx:
        sl_f, tp_f = se.lane_fracs(st, b.cost_pct, b.sl_min_steps)
        for r in st.hist:
            n_rows += 1
            worst_net = max(worst_net, abs(float(r["pnl"]) - (float(r["pnl_pct"]) - cf)))
            why = str(r.get("reason") or "").split(":")[0]
            if st.kind in ("base", "exit") and why == "sl":
                n_stop += 1
                worst_sl = max(worst_sl, abs(float(r["pnl_pct"]) + sl_f))
            elif st.kind == "base" and why == "tp":
                n_tp += 1
                worst_tp = max(worst_tp, abs(float(r["pnl_pct"]) - tp_f))
    # the chain's nets are the rows' nets
    st = max(b.by_idx, key=lambda x: len(x.hist))
    full = sorted((tuple(r.get(k) for k in se._ROW_KEYS) for r in st.hist if r.get("pnl_pct") is not None), key=lambda r: se.finite(r[0]))
    ch = b._chain_of(st, full)
    nets_chain = [x for _t, x in ch["main"]]
    # Several symbols can close at the same instant with different results. Check the
    # multiset of (time, net), rather than letting a timestamp-keyed dict overwrite them.
    from collections import Counter
    available = Counter((round(se.finite(r[0]), 3), round(se.finite(r[4]) - cf, 12)) for r in full)
    used = Counter((round(t, 3), round(x, 12)) for t, x in ch["main"])
    chain_ok = all(count <= available[key] for key, count in used.items())
    # a live close and a replay row with the same gross move
    st2 = b.by_idx[0]
    gross = 0.0031
    live = {"t": T0 + 99999.0, "symbol": "X", "side": "LONG", "pnl_pct": gross, "pnl": 1.0, "hold_s": 60.0, "reason": "tp", "set_id": st2.id, "client_id": "c1", "ours": True}
    b.on_live_close(live)
    row = st2.live[-1]
    live_net = se.finite(row["pnl"])
    ok = (n_rows > 200 and worst_net < 1e-12 and n_stop > 20 and worst_sl < 1e-9 and n_tp > 20 and worst_tp < 1e-9 and chain_ok and abs(live_net - (gross - cf)) < 1e-12)
    return ok, (f"{n_rows} rows: net = gross - {cf:.4f} (max deviation {worst_net:.1e}); {n_stop} stops at -SL (max deviation {worst_sl:.1e}); {n_tp} targets at +TP "
                f"(max deviation {worst_tp:.1e}); chain nets = row nets {chain_ok}; live close net {live_net:.5f} = gross {gross} - cost")


def the_statistics_relations_hold():
    """The stats' own relations, on a scored book with every window on: the Sets that passed Main are inside the Sets that passed Base, inside all; the valid Sets are
    the ones valid on the positive lane plus the ones valid on the Negative lane (which skips Main), inside the Sets that passed Base;
    the stage rows and the window rows count the same Sets (a stage's Sets are the ones that pass all of its windows, so never more than any one window);
    each stage's population is the stage before it; the pooled end figure is the pool of the valid Sets' long-window orders."""
    b = _scored_book(min_pf=1.1, mainMinN=1, mainMinMode="positive", setGateMinN=1, setGateMinMode="positive")
    pb = b.snapshot()["pfBase"]
    st, w = pb["stages"], pb["windows"]
    total = len(b.by_idx)
    reasons = {x.id: b.validation_reason(x) for x in b.by_idx}
    valid = [x for x in b.by_idx if not reasons[x.id]]
    gp = sum(x.gate_gp for x in valid)
    gl = sum(x.gate_gl for x in valid)
    rel = {
        "all Sets": pb["total"] == total == st["allSets"] == w["baseLong"]["pop"] == pb["unfiltered"]["sets"],
        "main <= base <= all, valid <= base": st["main"]["sets"] <= st["base"]["sets"] <= total and st["valid"]["sets"] <= st["base"]["sets"],
        "base stage = baseOnly": st["base"]["sets"] == pb["baseOnly"]["sets"] == pb["stageSets"]["base"],
        "Main figure uses Main PF-passing Sets": pb["main"]["sets"] == st["main"]["sets"] and pb["main"]["pf"] == st["main"]["pf"] and pb["main"]["n"] == st["main"]["n"],
        "all-lane Main pool is explicit": pb["mainAllLanes"]["sets"] == len(valid) and pb["mainAllLanes"]["n"] == sum(x.main_n for x in valid),
        "base stage <= each Base PF window": st["base"]["sets"] <= min(w["baseLong"]["sets"], w["baseShort"]["sets"]),
        "main stage <= each Main PF window": st["main"]["sets"] <= min(w["mainLong"]["sets"], w["mainShort"]["sets"]),
        "Main windows judge the Base-passing Sets": w["mainLong"]["pop"] == w["mainShort"]["pop"] == w["mainMinimal"]["pop"] == w["negative"]["pop"] == st["base"]["sets"],
        "Real judges the Main-passing Sets": w["real"]["pop"] == st["main"]["sets"],
        "Real Negative judges the Sets in a dip": w["realNeg"]["pop"] == w["negative"]["sets"],
        "valid = valid on the positive lane + valid on the Negative lane": st["valid"]["sets"] == pb["valid"] == len(valid) == st["real"]["sets"] + st["realNeg"]["sets"],
        "end figure = pool of the valid Sets' long windows": abs(pb["base"]["pf"] - round(gp / gl if gl > 0 else (99.0 if gp > 0 else 0.0), 4)) < 1e-9 and pb["base"]["n"] == sum(x.gate_n for x in valid),
        "shares": all(abs(w[k]["pct"] - (100.0 * w[k]["sets"] / w[k]["pop"] if w[k]["pop"] else 0.0)) < 0.011 for k in w),
        "validated share": abs(pb["validatedPct"] - round(100.0 * len(valid) / total, 2)) < 1e-9,
    }
    bad = [k for k, v in rel.items() if not v]
    return not bad, f"{total} Sets: valid {len(valid)}, Main {st['main']['sets']}, Base {st['base']['sets']}; relations broken: {bad or 0}"


def negative_lane_does_not_pollute_main_passing_pf():
    """The Negative fallback has its own population; it can pass while the positive Main PF fails."""
    b = SetBook()
    b.load(overlay(**{**SMALL, "setGateShortN": 0, "mainShortEnabled": False, "realGateEnabled": False, "mainNegEnabled": True}))
    a, c = [x for x in b.by_idx if x.kind == "base"][:2]
    b.by_idx = [a, c]
    b.sets = {x.id: x for x in b.by_idx}
    for x in (a, c):
        x.active, x.locked, x.last_error, x.gate_n, x.gate_pf, x.max_dd_s = True, False, "", 10, 2.0, 0.0
        x.gate_gp, x.gate_gl = 0.02, 0.01
        x.main_n = 10
    a.main_gp, a.main_gl = 0.02, 0.01
    a.stage = {"main": True, "short": True, "minimal": True, "pos": True, "neg": False, "negl": False}
    c.main_gp, c.main_gl = 0.01, 0.03
    c.stage = {"main": False, "short": True, "minimal": True, "pos": False, "neg": True, "negl": True}
    p = b.snapshot()["pfBase"]
    ok = (b.validation_lane(a) == "pos" and b.validation_lane(c) == "neg" and p["valid"] == 2
          and p["main"]["sets"] == 1 and p["main"]["pf"] == 2.0
          and p["mainAllLanes"]["sets"] == 2 and p["mainAllLanes"]["pf"] == 0.75)
    return ok, f"Main-pass PF {p['main']['pf']} on {p['main']['sets']}; all-lane PF {p['mainAllLanes']['pf']} on {p['mainAllLanes']['sets']}"


def debug_uses_each_window_pf_floor():
    """The trace must explain the same size-dependent gates that admit the Set."""
    import stage_debug as dbg
    p = replace(CFG_FULL, base_long=5, base_short=0, gate_min=1, floor=1.1,
                size_floors=((5, 2.0),), long_floor=2.0, lookback_s=1e9,
                max_dd_s=1e9, main_on=False, short_on=False, neg_on=False,
                base_min_n=0, main_min_n=0)
    tr = dbg.trace_set([10.0, 20.0, 30.0], [9.0, 19.0, 29.0], [0.03, -0.02, 0.01], p)
    last = tr['orders'][-1]
    ok = last['base_long_pf'] == 1.5 and last['base_stop'] == 'long' and not last['in_main'] and dbg.consistent(tr)
    return ok, f"PF={last['base_long_pf']} floor=2.0, stop={last['base_stop']}, consistent={dbg.consistent(tr)}"



def set_and_lane_ids_never_collide():
    """Every Set has one id, and the Sets its orders form (<id>:main, :real, :realneg) are distinct from it and from every other Set's: an Exit Set (<pack>:1m:sl<r>:st<n>:xt)
    never reads as its base twin, a trailing Set never as either, in the book, in the snapshot's index and in the client id the live order carries."""
    b = SetBook()
    b.load(overlay(**{"stagesEnabled": True, "exitIgnoreTp": True}))
    ids = [x.id for x in b.by_idx]
    derived = [x + suf for x in ids for suf in (se.MAIN_SUFFIX, se.REAL_SUFFIX, se.REALNEG_SUFFIX)]
    allids = ids + derived
    exits = [x for x in b.by_idx if x.kind == "exit"]
    twins = {x.id[:-3] for x in exits}
    base_ids = {x.id for x in b.by_idx if x.kind == "base"}
    idx = b.snapshot()["index"]
    snap_ids = [r["id"] for r in idx]
    ok = (len(set(ids)) == len(ids) and len(set(allids)) == len(allids) and all(x.id.endswith(":xt") for x in exits) and twins <= base_ids and not (set(ids) & set(derived))
          and snap_ids == ids and all(r["mainId"] == r["id"] + ":main" and r["realId"] == r["id"] + ":real" for r in idx) and len(exits) > 100)
    return ok, f"{len(ids)} Sets, {len(derived)} derived Set ids, all distinct: {len(set(allids)) == len(allids)}; {len(exits)} Exit Sets, each with a base twin: {twins <= base_ids}"


CHECKS = [
    ("stages.debug-uses-each-window-pf-floor", debug_uses_each_window_pf_floor),
    ("stages.replay-and-current-verdict-use-same-pf-rounding", replay_and_current_verdict_use_the_same_pf_rounding),
    ("stages.negative-lane-does-not-pollute-main-passing-pf", negative_lane_does_not_pollute_main_passing_pf),
    ("stages.every-window-is-judged-on-its-own", every_window_is_judged_on_its_own),
    ("stages.window-figures-pool-exactly-the-orders-each-window-reads", window_figures_pool_exactly_the_orders_each_window_reads),
    ("stages.the-plain-reading-equals-the-chain", the_plain_reading_equals_the_chain),
    ("stages.the-debug-trace-equals-the-engine", the_debug_trace_equals_the_engine),
    ("stages.the-chain-is-causal", the_chain_is_causal),
    ("stages.a-negative-window-of-cancelling-results-is-not-negative", a_negative_window_of_cancelling_results_is_not_negative),
    ("stages.the-chain-is-neutral-without-persistence-and-lifts-it-with", the_chain_is_neutral_without_persistence_and_lifts_it_with),
    ("stages.a-set-verdict-ignores-every-other-set", a_set_verdict_ignores_every_other_set),
    ("stages.the-statistics-relations-hold", the_statistics_relations_hold),
    ("stages.set-and-lane-ids-never-collide", set_and_lane_ids_never_collide),
    ("stages.costs-are-applied-once-and-lanes-sit-at-their-distances", costs_are_applied_once_and_lanes_sit_at_their_distances),
]
