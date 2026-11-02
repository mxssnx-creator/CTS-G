"""Pause after success: the rest a Set lane takes after a WINNING close, per indication (pack) and symbol.

Independent readings: the streaming lane (SetLane.step) against the replay loop (SetLane.scan), a slow rule that only reads the trades, causality of
the Auto choice (a result at or after a bar never moves the choice made on it), live (Pulse.symbol_backoff) against the replay's rule, and the
settings that reach the engine."""
from __future__ import annotations

import random
import types

from common import SMALL, T0, lane_cfg, overlay, synth_bars
import set_engine as se


def _book(**extra):
    b = se.SetBook()
    b.load(overlay(**SMALL, **extra))
    return b


def _signals(count, seed, every=7):
    r = random.Random(seed)
    dirs = [0] * count
    confs = [0.0] * count
    for i in range(10, count, every):
        dirs[i] = r.choice((-1, 1))
        confs[i] = 0.9
    return dirs, confs


def _base_set(b, step=3):
    return next(x for x in b.by_idx if x.kind == "base" and b.step_of(x) == step)


def _run_scan(st, bars, dirs, confs, cfg, pause=0, select=None):
    lane = se.SetLane(st, "T")
    lane.pause = pause
    cand = [i for i in range(len(bars)) if dirs[i] and confs[i] >= cfg["entry_conf"]]
    return lane.scan(0, len(bars), bars, dirs, confs, True, cfg, cand, se.BarIndex(bars), select_pause=select)


def _run_step(st, bars, dirs, confs, cfg, pause=0):
    lane = se.SetLane(st, "T")
    lane.pause = pause
    out = []
    for i, bar in enumerate(bars):
        done = lane.step(i, bar, dirs[i], confs[i], True, cfg)
        if done:
            out.append(done)
    return out


def rest_after_follows_the_rule():
    table = [(2, 0, True, 2), (2, 10, True, 10), (2, 10, False, 2), (5, 3, True, 5), (2, 10, None, 2)]
    got = [se.rest_after(c, p, bool(w)) for c, p, w, _ in table]
    want = [t[3] for t in table]
    return got == want, f"got={got} want={want}"


def scan_and_step_agree_for_every_pause():
    b = _book()
    st = _base_set(b)
    cfg = lane_cfg(cooldown=2, time_bars=40, scratch_bars=99999, scratch_min=9)
    bars = synth_bars(11, 2500, vol=0.004)
    dirs, confs = _signals(len(bars), 5, every=5)
    rows = {}
    ok = True
    for pause in (0, 1, 7, 25):
        a = _run_scan(st, bars, dirs, confs, cfg, pause)
        c = _run_step(st, bars, dirs, confs, cfg, pause)
        ka = [(t["entry_i"], t["exit_i"], t["reason"]) for t in a]
        kc = [(t["entry_i"], t["exit_i"], t["reason"]) for t in c]
        rows[pause] = len(ka)
        ok = ok and ka == kc and len(ka) > 20
    sel = _run_scan(st, bars, dirs, confs, cfg, 0, select=lambda j: 25)
    fixed = _run_scan(st, bars, dirs, confs, cfg, 25)
    ok = ok and [(t["entry_i"], t["exit_i"]) for t in sel] == [(t["entry_i"], t["exit_i"]) for t in fixed]
    return ok and rows[25] < rows[0], f"orders by pause (scan == step): {rows}; select_pause == fixed pause"


def pause_follows_wins_only():
    b = _book()
    st = _base_set(b)
    cfg = lane_cfg(cooldown=2, time_bars=40, scratch_bars=99999, scratch_min=9)
    bars = synth_bars(21, 4000, vol=0.004)
    dirs, confs = _signals(len(bars), 8, every=4)
    pause = 30
    trades = _run_scan(st, bars, dirs, confs, cfg, pause)
    short_after_loss = after_win_ok = 0
    bad = []
    for a, nxt in zip(trades, trades[1:]):
        gap = nxt["entry_i"] - a["exit_i"]
        if a["pnl"] > 0:
            if gap < pause + 1:
                bad.append((a["exit_i"], gap))
            else:
                after_win_ok += 1
        elif gap < pause + 1:
            short_after_loss += 1
        if a["pnl"] <= 0 and gap < cfg["cooldown"] + 1:
            bad.append(("cooldown", a["exit_i"], gap))
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return not bad and short_after_loss > 0 and wins > 5, f"trades={len(trades)} wins={wins} entries after a win obey the pause={after_win_ok}, shorter rest after a loss={short_after_loss}, violations={bad[:3]}"


def auto_choice_never_reads_the_future():
    """The pause chosen at an exit bar is the same whatever happens from that bar on (results closing at or after it are never read)."""
    b = _book(pauseMode="auto", pauseAutoMin=8, pauseAutoMax=60, histLookbackBars=3000)
    pack = b.packs[0]
    cfg = lane_cfg(cooldown=2, time_bars=40, scratch_bars=99999, scratch_min=9)
    nonzero = 0
    mismatch = []
    for seed in (3, 4, 5):
        full = synth_bars(seed, 2600, vol=0.004)
        dirs, confs = _signals(len(full), seed, every=4)
        cut = 1700
        plans = []
        for count in (cut, len(full)):
            bars = full[:count]
            d, c = dirs[:count], confs[:count]
            one = [i for i in range(count) if d[i] and c[i] >= cfg["entry_conf"]]
            plan = b._pause_plan("T", bars, T0, 0, count, cfg, {q: d for q in b.packs}, {q: c for q in b.packs}, {q: one for q in b.packs}, se.BarIndex(bars))
            plans.append(plan[pack][1])
        for j in range(60, cut, 11):
            if plans[0](j) != plans[1](j):
                mismatch.append((seed, j, plans[0](j), plans[1](j)))
            nonzero += plans[1](j) > 0
    return not mismatch and nonzero > 0, f"choices read only earlier results: mismatches={mismatch[:3]}; non-zero choices seen={nonzero}"


def live_rest_equals_the_replay_rule():
    import pulse_trader as pt
    b = _book(pauseMode="fixed", pauseFixedMin=15, pausePack={"general": 40})
    p = pt.Pulse.__new__(pt.Pulse)
    p.position_cost_pct = 0.12
    p.ignore_syms = {}
    p.sets = b
    win = p.symbol_backoff("A-USDT", 0.01, 300.0, "tp", set_lane=True, pack="indications")
    manual = p.symbol_backoff("A-USDT", 0.01, 300.0, "tp", set_lane=True, pack="general")
    loss = p.symbol_backoff("A-USDT", -0.01, 300.0, "sl", set_lane=True, pack="indications")
    cost_only = p.symbol_backoff("A-USDT", 0.0005, 300.0, "scratch+", set_lane=True, pack="indications")
    cd = b.cooldown_bars * 60.0
    ok = win == 15 * 60.0 and manual == 40 * 60.0 and loss == cd and cost_only == cd
    return ok, f"waits (s): win(fixed 15)={win} win(manual general 40)={manual} loss={loss} win-below-cost={cost_only} cooldown={cd}"


def settings_reach_the_engine_and_the_replay_key():
    off = _book()
    auto = _book(pauseMode="auto", pauseCandidates="3, 9;12,0,-4,9999", pauseAutoMin=40, pauseAutoMax=120)
    bad = _book(pauseMode="sideways", pauseFixedMin="x", pausePack={"general": 7, "indications": -3, "z": "q"})
    def key(book):
        return book.pause_mode, book.pause_candidates, book.pause_auto_min, book.pause_auto_max
    ok = (off.pause_mode == "off" and off.pause_minutes("general", "A") == 0
          and auto.pause_mode == "auto" and auto.pause_candidates == (3, 9, 12, se.PAUSE_MAX) and auto.pause_auto_min == 40 and auto.pause_auto_max == 120
          and bad.pause_mode == "off" and bad.pause_fixed == 0 and bad.pause_pack == {"general": 7} and bad.pause_minutes("general") == 7)
    sig_a, sig_b = off._evaluation_config, auto._evaluation_config
    return ok and sig_a != sig_b, f"off={key(off)} auto={key(auto)} bad={bad.pause_mode, bad.pause_pack}; replay key differs={sig_a != sig_b}"


CHECKS = [("pause." + fn.__name__.replace("_", "-"), fn) for fn in (
    rest_after_follows_the_rule, scan_and_step_agree_for_every_pause, pause_follows_wins_only,
    auto_choice_never_reads_the_future, live_rest_equals_the_replay_rule, settings_reach_the_engine_and_the_replay_key)]
