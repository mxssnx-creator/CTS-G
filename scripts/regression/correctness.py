"""Independent paper/execution, Axis, Set Block and price-feature regression oracles."""
import json
import math
import os
import random
import tempfile
import time
import types
from dataclasses import replace

from common import SMALL, T0, overlay
import pulse_trader as pt
from set_engine import SetBook
from set_block import SetBlockBook
from market_features import price_window, multi_price_windows


def paper_actual_isolation():
    b = SetBook()
    b.load(overlay(**{**SMALL, "setGateShortN": 0, "realGateN": 10}))
    st = b.by_idx[0]
    rows = [(T0 + i * 60, "X", "LONG", .004, .0052, 30, "tp", st.id) for i in range(50)]
    b.set_processed(st, rows)
    keys = ("t", "symbol", "side", "pnl", "pnl_pct", "hold_s", "reason", "set_id")
    st.hist = [dict(zip(keys, r)) for r in rows]
    b._score_one(st, now=T0 + 3000)
    before = (st.gate_n, st.gate_pf, b._streams(st), b._pf_split()["stages"])
    for i in range(10):
        rec = {"set_id": st.id, "t": T0 + (40 + i) * 60, "symbol": "X", "side": "LONG",
               "pnl_pct": -.01, "lane": "pos", "hold_s": 30, "client_id": f"fill{i}"}
        b.on_live_close(rec)
        b.on_live_close(rec)
    after = (st.gate_n, st.gate_pf, b._streams(st), b._pf_split()["stages"])
    stats = b.execution_stats(st, "pos")
    ok = before == after and stats["n"] == 10 and stats["pf"] == 0 and not stats["allow"] and st.live_n == 10
    return ok, f"paper unchanged={before == after}; actual n={stats['n']} PF={stats['pf']} allow={stats['allow']}"


def config_change_invalidates_old_evidence():
    b = SetBook()
    ov = overlay(**SMALL)
    b.load(ov)
    st = b.by_idx[0]
    st.hist = [{"t": T0, "pnl_pct": .1, "pnl": .0988}]
    st.gate_pf = 99
    b.load(dict(ov))
    retained = bool(st.hist)
    b.load({**ov, "positionCostPct": .2})
    dirty = bool(st.last_error) and not st.hist and b.gate_reason(st) == "error" and st.gate_pf == 0
    return retained and dirty, f"identical settings retain data={retained}; cost change invalidates={dirty}"


def executed_restart_and_base_attribution():
    from trader import _isolated
    def run():
        p = pt.Pulse.__new__(pt.Pulse)
        p.sets = SetBook()
        p.sets.load(overlay(**{**SMALL, "realGateN": 200}))
        p.track_prefix = pt.TAG
        st = p.sets.by_idx[0]
        now = time.time()
        rows = []
        for i in range(220):
            row = {"t": now - 221 + i, "set_id": st.id, "symbol": "X", "side": "LONG", "lane": "pos",
                   "client_id": p.cid("o", set_id=st.id, set_idx=st.idx, pack=st.pack), "conn": pt.CONN_SHORT,
                   "pnl_pct": -.5, "base_entry": 100.0, "base_qty": 1.0, "exit": 101.0}
            rows.append(row)
        with open(pt.TRADES_PATH, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
            f.write("not json\n")
            for row in rows[-5:]:
                f.write(json.dumps(row) + "\n")
        p.seed_set_executions()
        p.seed_set_executions()
        stats = p.sets.execution_stats(st, "pos")
        return stats["n"] == 200 and st.live_n == 200 and all(abs(r["pnl_pct"] - .01) < 1e-12 for r in st.live), str(stats)
    return _isolated(run)


def axis_continuation_and_set_isolation():
    from coord_engine import Coordinator
    b = SetBook()
    b.load(overlay(**{**SMALL, "setGateShortN": 0, "mainGateEnabled": False,
                      "mainNegEnabled": False, "realGateEnabled": False}))
    p = pt.Pulse.__new__(pt.Pulse)
    p.sets, p.coord, p.position_cost_pct, p.open = b, Coordinator(), .12, {}
    p.coord.load({}, {"axisLastEnabled": True, "axisPrevEnabled": False, "axisPauseEnabled": False,
                      "axisContEnabled": True, "axisContMaxWindow": 2})
    pack = b.by_idx[0].pack
    bad, good = [x for x in b.by_idx if x.pack == pack and x.kind == "base"][:2]
    for st in b.by_idx:
        st.locked = st not in (bad, good)
    for st in (bad, good):
        st.gate_n, st.gate_pf, st.max_dd_s = 50, 3.0, 0
        st.score_at = T0
        st.exe = [(T0 - i * 60, -.01 if st is bad else .01) for i in range(10, 0, -1)]
    b.rank_key = lambda st: (100 if st is bad else 1,)
    chosen, _ = p._scope_pick(pack)
    p.open = {str(i): types.SimpleNamespace(set_id=good.id, lane="pos") for i in range(2)}
    capped, reason = p._scope_pick(pack)
    # Both lanes of one Set can validate. A failed/capped positive Axis must not
    # mask that SAME Set's independent Negative lane or relabel its execution.
    bad.locked = True
    b.main_on = b.neg_on = b.realst_on = True
    good.stage = {"pos": True, "negl": True, "main": True, "short": True, "real": True}
    good.neg = list(good.exe)
    good.exe = [(t, -.01) for t, _ in good.exe]
    alternate, lane, blocked = p._scope_choice(pack)
    both = alternate is good and lane == "neg" and not blocked
    return chosen is good and capped is None and bool(reason) and p.coord.slot_cap(0, 3) == 2 and both, f"independent alternative={chosen is good}; same-Set capacity={capped is None}; same-Set Negative fallback={both}"


def _block_cfg():
    return types.SimpleNamespace(max_stack=3, window=10, min_samples=3, volume_ratio=1.0,
                                 default_min_pf=1.25, pf_ratio=.8, pause_ratio=1)


def _parent(i=0, side="LONG"):
    return pt.Position("X", side, 1, 100, T0 + i * 180, 99, 102, 100, set_id="pack:1m:sl1:st4",
                       lane="pos", client_id=f"p{i}", base_entry=100, base_qty=1, set_hold_bars=10)


def block_independent_evidence_and_restart():
    """Block v2: every count keeps its OWN cumulative record, so one count's results never move another's. Three parents close; count 1 is observed in profit
    each time, count 2 at a loss: count 1 is admitted for a fourth parent (also after a restart from the state file), count 2 is not, another Set stays
    isolated, and the same parent is never counted twice (a duplicate close)."""
    with tempfile.TemporaryDirectory() as d:
        book = SetBlockBook(os.path.join(d, "b.json"))
        cfg = _block_cfg()
        for i in range(3):
            pos = _parent(i)
            book.observe(pos, 100.15, pos.opened_at + 10, cfg, .12, 86400)
            book.observe(pos, 100.4, pos.opened_at + 20, cfg, .12, 86400)
            book.close(pos, 100.35, pos.opened_at + 60, cfg, .12)
            book.close(pos, 100.35, pos.opened_at + 60, cfg, .12)  # duplicate close
        book = SetBlockBook(book.path)
        pos = _parent(4)
        first = book.candidate(pos, 100.4, pos.opened_at + 10, cfg, .12, 86400)
        pos.block_fills = [{"group": 1, "qty": 1, "price": 100.4}]
        second = book.candidate(pos, 100.4, pos.opened_at + 11, cfg, .12, 86400)
        other = replace(pos, set_id="other", block_fills=[])
        isolated = book.candidate(other, 100.4, pos.opened_at + 11, cfg, .12, 86400)
        led = book.ledgers["set|pack:1m:sl1:st4|pos|LONG"]
        counts = [int(led["paper"][str(n)][0]) for n in (1, 2)]
        pf1, pf2 = SetBlockBook.stats(led["paper"]["1"])["pf"], SetBlockBook.stats(led["paper"]["2"])["pf"]
        return (bool(first and first["group"] == 1) and second is None and isolated is None and counts == [3, 3] and pf1 >= 99 and pf2 < 1.0,
                f"first={first and first['group']}; count 2 blocked={second is None}; other Set blocked={isolated is None}; observations per count={counts}; PF count 1 / 2 = {pf1:.2f} / {pf2:.2f}")


def block_actual_and_paper_costs():
    """An executed add's record holds its NET result (notional weighted gross move less one PositionCost), once, as one observation."""
    with tempfile.TemporaryDirectory() as d:
        b = SetBlockBook(os.path.join(d, "b.json"))
        cfg = _block_cfg()
        pos = _parent()
        pos.block_fills = [{"group": 1, "qty": 1, "price": 100.2}, {"group": 1, "qty": 2, "price": 100.5}]
        b.observe(pos, 100.15, T0 + 1, cfg, .12, 86400)
        b.close(pos, 101, T0 + 60, cfg, .12)
        rec = b.ledgers["set|pack:1m:sl1:st4|pos|LONG"]["executed"]["1"]
        oracle = ((101 - 100.2) + 2 * (101 - 100.5)) / (100.2 + 2 * 100.5) - 0.0012
        return int(rec[0]) == 1 and abs(rec[1] - oracle) < 1e-12 and rec[2] == 0, f"one executed observation, net {rec[1]:.9f} oracle {oracle:.9f}"


def block_has_no_window_and_counts_do_not_borrow():
    """No window (owner 2026-11-03): a record is the cumulative sum of EVERY observation. 600 losses followed by 700 gains read PF = all gains / all losses (an
    old 25-row window would read 99); PF 1.0 neutral on all-zero results, 0 empty; a count's record is its own, and the legacy windows are not read."""
    import block_rules as br
    rec = br.new_record()
    for i in range(600):
        br.add_obs(rec, -0.001)
    for i in range(700):
        br.add_obs(rec, 0.001)
    flat, empty = br.new_record(), br.new_record()
    for i in range(5):
        br.add_obs(flat, 0.0)
    recent_only = br.new_record()
    for i in range(25):
        br.add_obs(recent_only, 0.001)
    src = open(os.path.join(os.path.dirname(pt.__file__), "set_block.py")).read()
    ok = (int(rec[0]) == 1300 and abs(br.record_pf(rec) - 700 / 600) < 1e-9 and br.record_pf(recent_only) == 99.0 and br.record_pf(flat) == 1.0 and br.record_pf(empty) == 0.0
          and "lookback_s <=" not in src and "block.window" not in src and "[-block.window" not in src)
    return ok, f"1,300 observations -> PF {br.record_pf(rec):.4f} (all gains / all losses {700 / 600:.4f}); 25 wins {br.record_pf(recent_only)}; all-zero {br.record_pf(flat)}; empty {br.record_pf(empty)}"


def block_engine_matches_a_plain_reading_of_the_rules():
    """Eight scripted parents of one Set (every count reached at +0.15 % and +0.40 %, exits from a fixed list): SetBlockBook (observe / candidate / close, fills
    recorded as the trader does) fires exactly the counts a plain-loop reading of the written rules fires - cumulative records, min observations 2, the PF
    threshold of the stack each count creates, one add per count, Additive volumes (ratios 0.3 / 0.6 / 0.9 of V0). No window: the 8th parent still reads
    every earlier parent."""
    from block_engine import calculate_block_minimum_profit_factor
    import block_rules as br
    exits = [100.9, 100.0, 101.2, 100.3, 100.8, 100.1, 101.0, 100.6]
    cf = 0.0012
    ratios = [0.3, 0.6, 0.9]
    with tempfile.TemporaryDirectory() as d:
        book = SetBlockBook(os.path.join(d, "b.json"))
        book.cfg = br.clean_cfg({"blockCounts": [{"step": 1, "ratio": r} for r in ratios], "blockMinObservations": 2, "blockProfitFactorRatio": 0.8, "blockRelations": ["set"], "blockSkipFirst": 0})
        blk = _block_cfg()
        got, qtys = [], []
        for i, ex in enumerate(exits):
            pos = _parent(i)
            t0 = pos.opened_at
            fired = []
            for step, px in enumerate((100.15, 100.4)):
                book.observe(pos, px, t0 + 10 + step * 10, blk, .12, 86400)
                while True:
                    cand = book.candidate(pos, px, t0 + 11 + step * 10, blk, .12, 86400)
                    if cand is None:
                        break
                    pos.block_fills.append({"group": cand["group"], "qty": cand["qty"], "price": px})
                    fired.append(cand["group"])
                    qtys.append(round(cand["qty"], 6))
            book.close(pos, ex, t0 + 60, blk, .12)
            got.append(sorted(fired))
    # the plain reading
    rec = {n: [0, 0.0, 0.0] for n in (1, 2, 3)}
    done = {n: [0, 0.0, 0.0] for n in (1, 2, 3)}          # the executed adds' own records
    want, wq = [], []
    entry_px = {1: 100.15, 2: 100.4, 3: 100.4}

    def plain_pf(r_):
        return r_[1] / r_[2] if r_[2] > 0 else (99.0 if r_[1] > 0 else (1.0 if r_[0] else 0.0))

    def put(r_, net):
        r_[0] += 1
        if net > 0:
            r_[1] += net
        elif net < 0:
            r_[2] += -net
    for ex in exits:
        fired = []
        for n in (1, 2, 3):
            thr = max(1.25, calculate_block_minimum_profit_factor(1.25, 0.8, sum(ratios[k - 1] for k in fired) + ratios[n - 1]))
            if rec[n][0] >= 2 and plain_pf(rec[n]) + 1e-9 >= thr and not (done[n][0] >= 2 and plain_pf(done[n]) + 1e-9 < thr):
                fired.append(n)
                wq.append(round(ratios[n - 1] * 1.0, 6))
        want.append(sorted(fired))
        for n in (1, 2, 3):
            net = (ex / entry_px[n] - 1) - cf
            put(rec[n], net)
            if n in fired:
                put(done[n], net)
    return got == want and qtys == wq and any(want), f"counts fired per parent: engine {got}, plain reading {want}; add volumes {qtys}"


def block_carry_sizes_the_next_parent_of_the_relation():
    """With blockCarry on, a parent that fired count 2 and closed at a net loss raises its relation's level: the NEXT parent of that Set opens at V0 x (1 + r1 +
    r2) (SetBlockBook.entry_scale, used by Pulse.place), another Set at 1.0, carry off at 1.0; after a parent whose net brings the relation back to or above zero
    the scale returns to 1.0 and the relation pauses (no add) for `blockPauseSteps` parents."""
    import block_rules as br
    out = {}
    for carry in (True, False):
        with tempfile.TemporaryDirectory() as d:
            book = SetBlockBook(os.path.join(d, "b.json"))
            book.cfg = br.clean_cfg({"blockCarry": carry, "blockPauseSteps": 1, "blockMinObservations": 1, "blockRelations": ["set"], "blockSkipFirst": 0,
                                     "blockCounts": [{"step": 1, "ratio": 0.3}, {"step": 1, "ratio": 0.4}, {"step": 1, "ratio": 0.5}]})
            blk = _block_cfg()
            pos = _parent(0)
            book.observe(pos, 100.15, pos.opened_at + 10, blk, .12, 86400)
            book.observe(pos, 100.3, pos.opened_at + 20, blk, .12, 86400)
            pos.block_fills = [{"group": 1, "qty": 0.3, "price": 100.15}, {"group": 2, "qty": 0.4, "price": 100.3}]
            book.close(pos, 99.0, pos.opened_at + 60, blk, .12)                      # a net loss with count 2 fired
            lost = (round(book.entry_scale(pos.set_id, "pos", "LONG", "X", blk), 4), round(book.entry_scale("other", "pos", "LONG", "X", blk), 4))
            big = _parent(1)
            book.observe(big, 100.15, big.opened_at + 10, blk, .12, 86400)
            book.close(big, 103.0, big.opened_at + 60, blk, .12)                     # a large gain: the net is back above zero
            back = round(book.entry_scale(pos.set_id, "pos", "LONG", "X", blk), 4)
            nxt = _parent(2)
            book.observe(nxt, 100.4, nxt.opened_at + 10, blk, .12, 86400)
            book.candidate(nxt, 100.4, nxt.opened_at + 11, blk, .12, 86400)
            paused = max(int(led["life"].get("pause", 0)) for led in book.ledgers.values())    # the relation's pause counter (parents left)
            out[carry] = (lost, back, paused)
    ok = out[True] == ((1.7, 1.0), 1.0, 1) and out[False] == ((1.0, 1.0), 1.0, 0)
    return ok, f"carry on: (scale after a loss, other Set), back at zero, paused {out[True]}; carry off {out[False]}"


def block_shipped_defaults_are_the_measured_ones():
    """Block v2 defaults (measured 2026-11-04, stack_eval block2 on windows C and B, 4,400 ratio combinations and 12,960 settings each; the sealed third read once for
    four finalists): per-symbol evidence, three counts 3 PositionCosts apart, the first count only collects evidence (skip 1), counts 2 and 3 add 0.5 x the original
    volume (Additive), no minimum observations, no carry, no window. Every shipped overlay carries them and the engine's own defaults equal them."""
    import json as _j
    import block_rules as br
    from common import PULSE
    d = br.BlockCfg()
    want_engine = (d.levels() == [3.0, 6.0, 9.0] and [d.target_ratio(n) for n in (1, 2, 3)] == [0.5, 0.5, 0.5] and d.relations == ("symbol",) and d.skip_first == 1
                   and d.min_obs == 1 and d.carry is False and d.stack_type == "additive")
    seen = {}
    for v in ("bingx", "bybit"):
        for c in ("x01", "x02"):
            ov = _j.load(open(os.path.join(PULSE, f"overlay-{v}-{c}.json")))
            cfg = br.clean_cfg(ov)
            seen[f"{v}-{c}"] = (cfg.levels(), [cfg.target_ratio(n) for n in (1, 2, 3)], cfg.relations, cfg.skip_first, cfg.min_obs, cfg.carry, cfg.stack_type, "blockEvidenceScope" in ov)
    ok = want_engine and all(x == ([3.0, 6.0, 9.0], [0.5, 0.5, 0.5], ("symbol",), 1, 1, False, "additive", False) for x in seen.values())
    return ok, f"engine defaults equal the measured ones: {want_engine}; shipped overlays {sorted(set(map(str, seen.values())))}"


def block_stack_types_and_per_count_ratios():
    """Additive: each count adds its OWN ratio x V0 (0.2, 0.5, 1.0 here, bounded 0.1 .. 1.0); Shared: one budget R x V0 split equally over the enabled counts
    (R 1.0 over three -> 1/3 each, over two (one off) -> 1/2 each); a count that is off adds nothing; the first `skip_first` counts are never added; the steps
    are cumulative (2 + 3 + 1 = triggers at 2, 5 and 6 PositionCosts); out-of-range inputs are bounded and unreadable entries dropped."""
    import block_rules as br
    raw = {"blockCounts": [{"step": 2, "ratio": 0.2}, {"step": 3, "ratio": 0.5}, {"step": 1, "ratio": 9.0}, {"step": "x", "ratio": 0.3}]}
    add = br.clean_cfg(raw)
    sh = br.clean_cfg({**raw, "blockStackType": "shared", "blockSharedTotal": 1.0})
    off = br.clean_cfg({**raw, "blockStackType": "shared", "blockCounts": [{"step": 2, "ratio": .2}, {"step": 3, "ratio": .5, "on": False}, {"step": 1, "ratio": .9}]})
    skip = br.clean_cfg({**raw, "blockSkipFirst": 1})
    ok = (len(add.counts) == 3 and [add.target_ratio(n) for n in (1, 2, 3)] == [0.2, 0.5, 1.0] and add.levels() == [2.0, 5.0, 6.0]
          and all(abs(sh.target_ratio(n) - 1 / 3) < 1e-9 for n in (1, 2, 3)) and abs(off.target_ratio(1) - 0.5) < 1e-9 and off.target_ratio(2) == 0.0
          and not off.active(2) and skip.active(2) and not skip.active(1) and add.target_ratio(9) == 0.0)
    return ok, f"additive {[add.target_ratio(n) for n in (1, 2, 3)]}, levels {add.levels()}; shared {[round(sh.target_ratio(n), 4) for n in (1, 2, 3)]}; one off {[off.target_ratio(n) for n in (1, 2, 3)]}"


def block_relations_are_independent_and_all_must_agree():
    """Relations (symbol / indication / set / step) hold their own ledgers: a record under one relation never reads another's. With two relations enabled a
    count adds only when BOTH pass; the key of the step relation carries the pack and TP step, the set relation the Set, lane and side."""
    import block_rules as br
    with tempfile.TemporaryDirectory() as d:
        book = SetBlockBook(os.path.join(d, "b.json"))
        cfg = _block_cfg()
        book.cfg = br.clean_cfg({"blockRelations": ["set", "step"], "blockMinObservations": 3, "blockSkipFirst": 0, "blockCounts": [{"step": 1, "ratio": 0.5}] * 2})
        pos = _parent(1)
        keys = book.rel_keys(book.cfg, pos)
        for k in keys:
            book.ledger(k, create=True)
        for _ in range(4):
            br.add_obs(book.ledgers[keys[0]]["paper"].setdefault("1", br.new_record()), 0.01)          # only the set relation has proof
        one = book.candidate(replace(pos, client_id="a"), 100.15, T0 + 700, cfg, .12, 86400)
        for _ in range(4):
            br.add_obs(book.ledgers[keys[1]]["paper"].setdefault("1", br.new_record()), 0.01)          # now the step relation too
        both = book.candidate(replace(pos, client_id="b"), 100.15, T0 + 701, cfg, .12, 86400)
        sym = br.relation_keys(br.clean_cfg({"blockRelations": ["symbol", "indication"]}), "pack:1m:sl1:st4", "pos", "LONG", "X", "pack", 4)
        ok = one is None and both is not None and both["group"] == 1 and keys == ["set|pack:1m:sl1:st4|pos|LONG", "step|pack|4|pos|LONG"] and sym == ["symbol|X|LONG", "indication|pack|pos|LONG"]
        return ok, f"one relation proven -> {one and one['group']}; both -> {both and both['group']}; keys {keys + sym}"


def block_lifecycle_carries_until_net_is_positive_then_pauses():
    """The opt-in lifecycle per relation: a net-losing parent that fired count 2 raises the relation's level to 2 (the next parent opens at V0 x (1 + r1 + r2));
    the level holds while the relation's net stays negative; when the net is back at or above zero the level resets and the relation pauses `pause_steps`
    parents (no add, evidence keeps collecting), counting one parent down at each close; without carry nothing is inherited (scale 1.0)."""
    import block_rules as br
    cfg = br.clean_cfg({"blockCarry": True, "blockPauseSteps": 2, "blockCounts": [{"step": 1, "ratio": 0.3}, {"step": 1, "ratio": 0.4}, {"step": 1, "ratio": 0.5}]})
    life = br.new_life()
    life = br.life_after_close(cfg, life, -0.02, 2)
    after_loss = (life["level"], round(br.carried_scale(cfg, life), 4))
    life = br.life_after_close(cfg, life, -0.005, 0)
    still = (life["level"], life["pause"])
    life = br.life_after_close(cfg, life, +0.03, 0)
    reset = (life["level"], life["pause"], life["net"])
    life = br.life_after_close(cfg, life, +0.01, 0)
    one_down = life["pause"]
    life = br.life_after_close(cfg, life, +0.01, 0)
    none = br.clean_cfg({"blockCarry": False})
    ok = (after_loss == (2, 1.7) and still == (2, 0) and reset == (0, 2, 0.0) and one_down == 1 and life["pause"] == 0
          and br.carried_scale(none, {"level": 2}) == 1.0)
    return ok, f"after a loss firing 2: level/scale {after_loss}; a second loss keeps it {still}; net back positive {reset}; pause counts down {one_down} -> {life['pause']}; no carry scale {br.carried_scale(none, {'level': 2})}"


def block_parent_unchanged_end_to_end():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        p.equity = 1000
        st = p.sets.by_idx[0]
        pos.set_id, pos.lane, pos.pack = st.id, "pos", st.pack
        pos.base_entry, pos.base_qty, pos.set_hold_bars = pos.entry, pos.qty, 10
        p.sets.gate_reason = lambda st: ""
        p.sets.main_on = p.sets.neg_on = p.sets.realst_on = True
        st.stage = {"pos": True, "negl": True}
        p.ensure_controls = lambda pos: None
        p.block.min_samples = 3
        p.block.pause_ratio = 0
        p.block.volume_ratio = 1
        import block_rules as _br
        p.set_block.cfg = p.block_cfg = _br.clean_cfg({"blockCounts": [{"step": 1, "ratio": 1.0}] * 3, "blockRelations": ["set"], "blockSkipFirst": 0, "blockMinObservations": 3})
        before = (pos.base_entry, pos.base_qty, pos.sl, pos.tp, pos.set_hold_bars, pos.opened_at)
        now = time.time()
        for i in range(3):
            old = replace(pos, client_id=f"history{i}", block_fills=[], block_pending={})
            p.set_block.observe(old, 100.15, now - 300 + i * 60, p.block, .12, 86400)
            p.set_block.close(old, 101, now - 270 + i * 60, p.block, .12)
        p.px[SYM] = 100.15
        # A different lane's eligibility cannot authorize this parent's add.
        st.stage = {"pos": False, "negl": True}
        p.maybe_set_block_adds()
        isolated = not pos.block_fills
        st.stage = {"pos": True, "negl": True}
        p.maybe_set_block_adds()
        after = (pos.base_entry, pos.base_qty, pos.sl, pos.tp, pos.set_hold_bars, pos.opened_at)
        grew = pos.qty > pos.base_qty and len(pos.block_fills) == 1
        saved = json.load(open(pt.OPEN_PATH))[SYM]
        recovered = pt.Position(**saved)
        p.close_pos(pos, 101, "tp", exchange=False)
        own = p.sets.sets[st.id].live[-1]
        rec = p.closed[-1]
        fee_rate = max(0.0, p.overlay["configuredPositionCostPct"] - p.slippage_allowance_pct) / 200
        base_cash = (101 - before[0]) * before[1] - (before[0] + 101) * before[1] * fee_rate
        add_cash = sum((101 - f["price"]) * f["qty"] - (f["price"] + 101) * f["qty"] * fee_rate for f in rec.block_fills)
        ok = isolated and grew and before == after and recovered.block_fills == pos.block_fills and abs(own["pnl_pct"] - .01) < 1e-12 and abs(rec.pnl - base_cash - add_cash) < 1e-10
        return ok, f"added={grew}, lane isolated={isolated}, parent unchanged={before == after}, base return={own['pnl_pct']:.6f}, aggregate cash error={rec.pnl-base_cash-add_cash:.2g}"


def drawdown_plain_oracle():
    rng = random.Random(15)
    closes = [100.0]
    for _ in range(300):
        closes.append(closes[-1] * (1 + rng.uniform(-.01, .01)))
    worst = 0
    for end in (100, 200, 300):
        data = closes[:end]
        for n, got in multi_price_windows(data).items():
            dd = [1 - data[k] / max(data[k - n + 1:k + 1]) for k in range(end - n, end)]
            du = [data[k] / min(data[k - n + 1:k + 1]) - 1 for k in range(end - n, end)]
            worst = max(worst, abs(got["drawdown"]-dd[-1]), abs(got["ulcer"]-math.sqrt(sum(x*x for x in dd)/n)),
                        abs(got["shortUlcer"]-math.sqrt(sum(x*x for x in du)/n)))
    flat = price_window([100] * 80, 20)
    return worst < 1e-12 and flat["ulcer"] == flat["shortUlcer"] == flat["efficiency"] == 0, f"12 windows vs quadratic oracle, max error={worst:.2g}; flat neutral"


def flat_rsi_and_relation_names():
    import indication_lib as lib
    import indication_extra as ix
    a = lib._rsi([100] * 100, 14)
    b = ix._rsi_series([100] * 100, 14)
    names = [f"{fam}_n{n}_t{tf}@DD20@LP3@I" for fam in ("ddresume", "ddpressure") for n in (5,8,14,21) for tf in (1,5,15,30)]
    return set(a) == set(b) == {50} and all(ix.known(n) for n in names), f"RSI flat=50; {len(names)} drawdown/last-part/inversion compositions resolve"


def fifty_symbol_processing():
    from common import synth_bars
    b = SetBook()
    b.load(overlay(**{**SMALL, "holdEnabled": False, "setStepAdapt": False,
                     **{k: True for k in ("stratShortRange", "stratTrend", "stratBreakout", "stratReversion", "stratFlow", "stratMulti")}}))
    for i in range(50):
        sym = f"S{i}-USDT"
        b.bars[sym] = synth_bars(i, 360, drift=.0005 if i % 2 else -.0005)
        b.bar_t[sym] = T0 - 60
    started = time.monotonic()
    b.replay_all(now=T0)
    checked = len(b.by_idx) * 50
    ids = [st.id for st in b.by_idx]
    ok = (b.progress.ready and b.progress.errors == 0 and b.progress.symbols_done == 50 and len(b.packs) == 8
          and len(ids) == len(set(ids)) and all(st.evaluated_at > 0 and not st.last_error for st in b.by_idx))
    return ok, f"50 symbols x {len(ids)} Sets = {checked} independent lanes, six hours each, {time.monotonic()-started:.2f}s"


def balance_sizing_and_block_budget():
    p = pt.Pulse.__new__(pt.Pulse)
    p.volume_factor, p.size_pct, p.size_max, p.available = .05, 1.0, 25.0, 10000
    p.dca = types.SimpleNamespace(enabled=False)
    p.block = types.SimpleNamespace(enabled=True, max_stack=6, volume_ratio=1.0)
    p.leverage_for = lambda c: 50
    amounts = []
    for bal in (10000, 20000, 100000):
        p.equity = bal
        amounts.append(p.sized_notional())
    p.equity = 10000
    budget = p.max_book_notional()
    return amounts == [5, 10, 25] and budget == 35, f"equity 10k/20k/100k -> notional {amounts}; six additive Block groups -> budget {budget}"


def frozen_hold_and_block_intent_failure():
    from lanes import _Env, SYM
    with _Env() as env:
        p, pos = env.p, env.pos
        st = p.sets.by_idx[0]
        pos.set_id, pos.base_entry, pos.base_qty, pos.set_hold_bars = st.id, pos.entry, pos.qty, 10
        st.hold_bars = 1
        p.software_exit = lambda *a: False
        p.last_closed_1m = {}
        calls = []
        p.close_pos = lambda *a, **k: calls.append(a[2])
        p.manage_set_lane(pos, pos.entry, time.time(), 120, 0)
        early = list(calls)
        p.manage_set_lane(pos, pos.entry, time.time(), 600, 0)
        p.sets.gate_reason = lambda st: ""
        p.set_block.candidate = lambda *a, **k: {"group": 1, "qty": pos.qty, "paper": {"pf": 2}}
        p.save_open_book = lambda: False
        p.px[SYM] = pos.entry * 1.002
        before = len(env.venue.posts)
        p.maybe_set_block_adds()
        return not early and calls == ["time"] and len(env.venue.posts) == before, f"entry hold kept={not early and calls == ['time']}; no order without persisted intent={len(env.venue.posts)==before}"


def dca_ladder_equals_a_plain_reading_and_a_worked_example():
    """The 12-step DCA simulator (stack_eval.dca_rungs) against a plain bar-by-bar reading on 400 random paths (rung level, first touching bar, cap on the whole
    add volume, indication gate, off switch) and against one worked example: a long at 100, stop distance 2 %, target 4 %: rungs at 0.1 / 0.2 x the stop
    (0.2 % and 0.4 %) of 0.25 and 0.5 V0 fill at 99.8 and 99.6 and exit at 104 together with the parent; each pays one PositionCost."""
    import stack_eval as sv
    rnd = random.Random(7)
    cf = 0.0012
    bad, filled = [], 0
    for trial in range(400):
        n = rnd.randint(40, 90)
        px, hi, lo = 100.0, [], []
        for _ in range(n):
            o = px
            px *= 1 + rnd.gauss(0, 0.002)
            hi.append(max(o, px) * (1 + abs(rnd.gauss(0, 0.0007))))
            lo.append(min(o, px) * (1 - abs(rnd.gauss(0, 0.0007))))
        side = rnd.choice((1, -1))
        e_i, i_end = rnd.randint(1, 10), rnd.randint(20, n - 1)
        entry, exit_px, sl_f = 100.0, 100.0 * (1 + side * rnd.uniform(-0.03, 0.05)), rnd.choice((0.01, 0.02, 0.035))
        cfg = {"n": rnd.choice((1, 3, 6, 12)), "f0": rnd.choice((0.05, 0.1, 0.3)), "fs": rnd.choice((0.05, 0.075, 0.1)), "m0": rnd.choice((0.1, 0.25, 0.5)),
               "g": rnd.choice((1.0, 1.3)), "max_expo": rnd.choice((2.0, 3.0, 4.0))}
        gated = set(rnd.sample(range(n), n // 3))
        stop_at = rnd.randint(0, n) if rnd.random() < 0.3 else n + 1

        def hit_np(i0, i1, lvl, d):
            for j in range(i0, i1):
                if (hi[j] >= lvl) if d > 0 else (lo[j] <= lvl):
                    return j
            return -1
        got = sv.dca_rungs(side, entry, exit_px, sl_f, e_i, i_end, cfg, cf, hit_np, (lambda j: j in gated) if trial % 2 else None, lambda j: j < stop_at)
        # the plain reading: bar by bar, no helper shared with the function under test
        want, used, start = [], 0.0, e_i + 1
        for k in range(cfg["n"]):
            dist = min(max((cfg["f0"] + k * cfg["fs"]) * sl_f, cf), 0.9 * sl_f)
            vol = cfg["m0"] * cfg["g"] ** k
            if used + vol > cfg["max_expo"] - 1.0 + 1e-12:
                vol = cfg["max_expo"] - 1.0 - used
                if vol < 0.05:
                    break
            lvl = entry * (1 - side * dist)
            j = start
            found = -1
            while j < i_end:
                touched = (lo[j] <= lvl) if side > 0 else (hi[j] >= lvl)
                if touched and (trial % 2 == 0 or j in gated):
                    found = j
                    break
                j += 1
            if found < 0 or found >= stop_at:
                break
            want.append((found, k, vol, (exit_px - lvl) / lvl * side - cf))
            used += vol
            start = found
        filled += len(want)
        if got != want:
            bad.append((trial, got[:2], want[:2]))
    ex = sv.dca_rungs(1, 100.0, 104.0, 0.02, 0, 10, {"n": 2, "f0": 0.1, "fs": 0.1, "m0": 0.25, "g": 2.0, "max_expo": 3.0}, cf,
                      lambda i0, i1, lvl, d: next((j for j in range(i0, i1) if [100.0, 100.0, 99.7, 99.5, 99.9, 100, 100, 100, 100, 100][j] <= lvl), -1))
    want_ex = [(2, 0, 0.25, (104.0 - 99.8) / 99.8 - cf), (3, 1, 0.5, (104.0 - 99.6) / 99.6 - cf)]
    ok = not bad and filled > 500 and all(abs(a[3] - b[3]) < 1e-12 and a[:3] == b[:3] for a, b in zip(ex, want_ex)) and len(ex) == 2
    return ok, f"400 random paths equal the plain reading ({filled} rungs filled){(' bad ' + str(bad[:2])) if bad else ''}; worked example {[(j, k, m, round(n, 6)) for j, k, m, n in ex]}"


CHECKS = [("correctness." + fn.__name__.replace("_", "-"), fn) for fn in (
    dca_ladder_equals_a_plain_reading_and_a_worked_example,
    paper_actual_isolation, config_change_invalidates_old_evidence, executed_restart_and_base_attribution,
    axis_continuation_and_set_isolation, block_independent_evidence_and_restart, block_actual_and_paper_costs,
    block_has_no_window_and_counts_do_not_borrow, block_engine_matches_a_plain_reading_of_the_rules, block_carry_sizes_the_next_parent_of_the_relation, block_shipped_defaults_are_the_measured_ones, block_stack_types_and_per_count_ratios, block_relations_are_independent_and_all_must_agree,
    block_lifecycle_carries_until_net_is_positive_then_pauses,
    block_parent_unchanged_end_to_end, drawdown_plain_oracle, flat_rsi_and_relation_names,
    fifty_symbol_processing, balance_sizing_and_block_budget, frozen_hold_and_block_intent_failure)]
