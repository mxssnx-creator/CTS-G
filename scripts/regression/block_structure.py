"""Block structure (owner 2026-11-11): every count on its own with its own step and ratio, the stack built from the parent's ORIGINAL volume, the evidence
kept per relation (per Set, and "Overall" = per pack and TP step) for the open parents, and the three rules that only remove adds (deficit guard, share of
the way to the target, parent types). Driven through the engine's own book (set_block.SetBlockBook) with plain positions; nothing connects."""
from __future__ import annotations

import os
import tempfile
import types

import block_rules as br
from set_block import SetBlockBook

COST = 0.1                      # percent: one PositionCost = 0.001 of price
BLOCK = types.SimpleNamespace(max_stack=3, volume_ratio=0.5, min_samples=1, pf_ratio=0.8, default_min_pf=1.1)


def _pos(set_id="general:L:1m:sl1.00:st20", symbol="A-USDT", side="LONG", cid="p1", base_qty=10.0, entry=100.0, tp=102.0):
    return types.SimpleNamespace(set_id=set_id, lane="pos", symbol=symbol, side=side, client_id=cid, opened_at=1000.0, base_entry=entry, base_qty=base_qty,
                                 qty=base_qty, entry=entry, tp=tp, block_fills=[], exit_fee_qty=0.0, exit_fee_usdt=0.0, base_entry_fee_usdt=None)


def _book(td, **ov):
    b = SetBlockBook(os.path.join(td, "b.json"))
    b.cfg = br.clean_cfg({"blockCounts": [{"step": 1, "ratio": 0.5, "on": True}, {"step": 2, "ratio": 1.0, "on": True}, {"step": 3, "ratio": 0.25, "on": True}],
                          "blockSkipFirst": 0, "blockRelations": ["set"], "blockMinObservations": 1, **ov})
    return b


def _teach(b, pos, exit_px, now=2000.0, top=100.7):
    """One closed parent whose price reached `top` (so the counts up to that level observed) and then closed at exit_px."""
    b.observe(pos, top, now - 100.0, BLOCK, COST, 3600.0)
    b.close(pos, exit_px, now, BLOCK, COST)


def _fill(pos, row, price):
    pos.block_fills.append({"group": row["group"], "qty": row["qty"], "price": price})
    pos.entry = (pos.qty * pos.entry + row["qty"] * price) / (pos.qty + row["qty"])
    pos.qty += row["qty"]


def every_count_is_independent_and_stacks_from_the_original_volume():
    """Counts 1 / 2 / 3 at 1 / 3 / 6 PositionCosts (steps 1 + 2 + 3) with ratios 0.5 / 1.0 / 0.25. A count adds its OWN ratio x the parent's original volume
    (never a share of the grown position), only once, only while the price is at its own level, and only on its own evidence: with count 2's record losing,
    counts 1 and 3 still add and count 2 does not; the stack is V0 x (1 + 0.5 + 0.25). Shared stacking splits one budget over the counts that are on."""
    with tempfile.TemporaryDirectory() as td:
        b = _book(td)
        cfg = b.cfg
        levels = cfg.levels()
        _teach(b, _pos(cid="t1"), 101.5)                       # every count observed a winning add
        led = b.ledgers["set|general:L:1m:sl1.00:st20|pos|LONG"]
        led["paper"]["2"] = [3, 0.001, 0.02]                   # count 2's own record is losing; counts 1 and 3 keep theirs
        pos = _pos(cid="p2")
        seen = []
        below = b.candidate(pos, 100.05, 3000.0, BLOCK, COST, 3600.0, floor=1.1)          # under count 1's level: nothing
        r1 = b.candidate(pos, 100.12, 3001.0, BLOCK, COST, 3600.0, floor=1.1)
        seen.append((r1["group"], r1["qty"]))
        _fill(pos, r1, 100.12)
        again = b.candidate(pos, 100.2, 3002.0, BLOCK, COST, 3600.0, floor=1.1)           # count 1 is full, count 2 not reached: nothing
        at2 = b.candidate(pos, 100.35, 3003.0, BLOCK, COST, 3600.0, floor=1.1)            # count 2 reached but its own record loses: nothing
        r3 = b.candidate(pos, 100.65, 3004.0, BLOCK, COST, 3600.0, floor=1.1)
        seen.append((r3["group"], r3["qty"]))
        _fill(pos, r3, 100.65)
        after = b.candidate(pos, 100.9, 3005.0, BLOCK, COST, 3600.0, floor=1.1)
        shared = br.clean_cfg({"blockCounts": [{"step": 1, "ratio": 0.5, "on": True}, {"step": 2, "ratio": 1.0, "on": False}, {"step": 3, "ratio": 0.25, "on": True}],
                               "blockStackType": "shared", "blockSharedTotal": 1.0})
    ok = (levels == [1.0, 3.0, 6.0] and below is None and seen == [(1, 5.0), (3, 2.5)] and again is None and at2 is None and after is None
          and abs(pos.qty - 10.0 * (1 + 0.5 + 0.25)) < 1e-9 and [shared.target_ratio(n) for n in (1, 2, 3)] == [0.5, 0.0, 0.5])
    return ok, f"levels {levels}; adds (count, qty of a base volume 10) {seen}; final volume {pos.qty}; shared ratios {[shared.target_ratio(n) for n in (1, 2, 3)]}"


def six_counts_one_step_apart_run_six_independent_procedures():
    """Counts 1..6, each one PositionCost after the one before (levels 1..6), ratios 0.1 / 0.2 / 0.3 / 0.4 / 0.5 / 1.0. Earlier parents (the previous
    positions of the group) taught every count its own record: counts 1, 3, 4 and 6 won, counts 2 and 5 lost. A new parent that runs through all six levels
    then gets exactly the adds of counts 1, 3, 4 and 6, in order, each its own ratio x the ORIGINAL volume (1, 3, 4, 10 of a base 10), whatever the position
    has grown to; the stack ends at V0 x (1 + 0.1 + 0.3 + 0.4 + 1.0). The threshold a count must clear rises with the volume already stacked (its own
    ratio on top), never with another count's record. The statistics list every count of the group with its own observations and PF, and the close of
    the parent adds one observation to each count it reached and one executed result to each count it added, in that group only. Shared stacking gives
    the six counts one budget in equal parts."""
    ratios = [0.1, 0.2, 0.3, 0.4, 0.5, 1.0]
    table = [{"step": 1, "ratio": r, "on": True} for r in ratios]
    with tempfile.TemporaryDirectory() as td:
        b = _book(td, blockCounts=table, blockRelations=["set", "step"])
        cfg = b.cfg
        keys = ["set|general:L:1m:sl1.00:st20|pos|LONG", "step|general|20|pos|LONG"]
        for k in keys:                                          # what the group's previous positions left behind, count by count
            b.ledgers[k] = {"paper": {str(n): ([4, 0.03, 0.002] if n in (1, 3, 4, 6) else [4, 0.002, 0.03]) for n in range(1, 7)}, "executed": {},
                            "life": br.new_life(), "updated": 0.0}
        other = "step|general|12|pos|LONG"
        b.ledgers[other] = {"paper": {str(n): [9, 0.0, 0.09] for n in range(1, 7)}, "executed": {}, "life": br.new_life(), "updated": 0.0}   # another group: must not matter
        pos = _pos(cid="six", tp=103.0)
        adds, thresholds = [], []
        for step_no, price in enumerate([100.11, 100.21, 100.31, 100.41, 100.51, 100.61, 100.75], start=1):
            while True:
                row = b.candidate(pos, price, 3000.0 + step_no, BLOCK, COST, 3600.0, floor=1.1)
                if row is None:
                    break
                adds.append((row["group"], round(row["qty"], 9), price))
                thresholds.append(round(row["minPf"], 4))
                _fill(pos, row, price)
        snap = b.snapshot(4000.0, BLOCK, COST, 3600.0)
        rows = {(r["lane"], r["group"]): r for r in snap["lanes"]}
        before = {k: {n: list(v) for n, v in b.ledgers[k]["paper"].items()} for k in keys}
        b.close(pos, 100.9, 4100.0, BLOCK, COST)
        after = {k: b.ledgers[k] for k in keys}
        shared = br.clean_cfg({"blockCounts": table, "blockStackType": "shared", "blockSharedTotal": 1.5})
    want_adds = [(1, 1.0, 100.11), (3, 3.0, 100.31), (4, 4.0, 100.41), (6, 10.0, 100.61)]
    obs_ok = all(after[k]["paper"][str(n)][0] == before[k][str(n)][0] + 1 for k in keys for n in range(1, 7))
    exec_ok = all(sorted(after[k]["executed"]) == ["1", "3", "4", "6"] and all(v[0] == 1 for v in after[k]["executed"].values()) for k in keys)
    stats_ok = all(rows[(k, n)]["paper"]["n"] == 4 and (rows[(k, n)]["paper"]["pf"] > 1.1) == (n in (1, 3, 4, 6)) for k in keys for n in range(1, 7))
    ok = (cfg.levels() == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0] and adds == want_adds and abs(pos.qty - 10.0 * (1 + 0.1 + 0.3 + 0.4 + 1.0)) < 1e-9
          and thresholds == sorted(thresholds) and thresholds[0] >= 1.1 and obs_ok and exec_ok and stats_ok and b.ledgers[other]["paper"]["1"][0] == 9
          and all(abs(shared.target_ratio(n) - 0.25) < 1e-12 for n in range(1, 7)) and snap["counts"] == [{"n": i + 1, "step": 1.0, "ratio": r, "on": True} for i, r in enumerate(ratios)])
    return ok, f"levels {cfg.levels()}; adds (count, qty, price) {adds}; thresholds {thresholds}; final volume {pos.qty}; observations +1 per count {obs_ok}; executed only for the added counts {exec_ok}"


def evidence_is_kept_per_set_and_overall_for_the_open_parents():
    """Relations. "set": only this Set's own parents teach it; another Set of the same pack and TP step does not. "step" (Overall): every Set of the pack and
    step, lane and direction teaches one record, another step or direction does not. Both selected: both must agree. Observations come from every open parent
    (also one that gets no add), each count once per parent, measured from the price an add would have had when the count was first seen reached (a jump over
    two levels records both at the price after the jump: that is what an order sent then would have paid) to that parent's exit."""
    twin, other_step, short = "general:L:1m:sl2.00:st20", "general:L:1m:sl1.00:st12", "general:S:1m:sl1.00:st20"
    out = {}
    with tempfile.TemporaryDirectory() as td:
        for label, rel, teacher_set, teacher_side in (("set <- own", ["set"], "general:L:1m:sl1.00:st20", "LONG"), ("set <- twin", ["set"], twin, "LONG"),
                                                       ("step <- twin", ["step"], twin, "LONG"), ("step <- other step", ["step"], other_step, "LONG"),
                                                       ("step <- other direction", ["step"], short, "SHORT"),
                                                       ("set+step <- twin", ["set", "step"], twin, "LONG"), ("set+step <- own", ["set", "step"], "general:L:1m:sl1.00:st20", "LONG")):
            b = _book(os.path.join(td, label.replace(" ", "")) if os.makedirs(os.path.join(td, label.replace(" ", "")), exist_ok=True) is None else td, blockRelations=rel)
            if teacher_side == "LONG":
                _teach(b, _pos(set_id=teacher_set, cid="t"), 101.5)
            else:
                t = _pos(set_id=teacher_set, side="SHORT", cid="t", tp=98.0)
                b.observe(t, 99.3, 1900.0, BLOCK, COST, 3600.0)
                b.close(t, 98.5, 2000.0, BLOCK, COST)
            out[label] = b.candidate(_pos(cid="p"), 100.12, 3000.0, BLOCK, COST, 3600.0, floor=1.1) is not None
        b = _book(td, blockRelations=["step"])
        p = _pos(cid="obs")
        b.observe(p, 100.35, 1500.0, BLOCK, COST, 3600.0)       # reached counts 1 and 2 (1 and 3 costs), not 3
        b.observe(p, 100.35, 1501.0, BLOCK, COST, 3600.0)       # the same level again: still one observation per count
        b.close(p, 100.0, 2000.0, BLOCK, COST)
        rec = b.ledgers["step|general|20|pos|LONG"]["paper"]
    want = {"set <- own": True, "set <- twin": False, "step <- twin": True, "step <- other step": False, "step <- other direction": False,
            "set+step <- twin": False, "set+step <- own": True}
    lost = (100.35 - 100.0) / 100.35 + 0.001
    obs_ok = sorted(rec) == ["1", "2"] and rec["1"][0] == 1 and rec["2"][0] == 1 and abs(rec["1"][2] - lost) < 1e-9 and abs(rec["2"][2] - lost) < 1e-9
    return out == want and obs_ok, f"an add is allowed: {out}; observations of a parent that reached two counts: {dict(rec)}"


def the_deficit_guard_holds_adds_until_paper_adds_earn_the_deficit_back():
    """blockGuard: after executed adds have lost (the book's cumulative add net below zero) no add is sent; the add that would have gone is followed on paper
    from its price, once per count and parent; a parent whose paper add loses deepens nothing and resumes nothing; when the paper adds have earned the deficit
    back the record restarts at zero and the next add is sent. The record survives a restart; with the guard off the same book keeps adding. The guard is
    the BOOK's: the losing add was on symbol A, the held ones are on symbol B, whose own records are clean (a relation's own executed record already stops
    that relation; it never spoke for the others)."""
    with tempfile.TemporaryDirectory() as td:
        b = _book(td, blockGuard=True, blockRelations=["symbol"], blockCounts=[{"step": 1, "ratio": 1.0, "on": True}])
        _teach(b, _pos(cid="t1"), 101.5)
        _teach(b, _pos(cid="t2", symbol="B-USDT"), 101.5)
        p1 = _pos(cid="p1")
        r = b.candidate(p1, 100.12, 3000.0, BLOCK, COST, 3600.0, floor=1.1)
        _fill(p1, r, 100.12)
        b.close(p1, 99.6, 3100.0, BLOCK, COST)                 # the add lost about 0.62 %: the book is in deficit
        deficit = b.guard["net"]
        p2 = _pos(cid="p2", symbol="B-USDT")
        held = b.candidate(p2, 100.12, 3200.0, BLOCK, COST, 3600.0, floor=1.1)
        held_again = b.candidate(p2, 100.2, 3201.0, BLOCK, COST, 3600.0, floor=1.1)
        shadow_px = b.parents[b.key(p2)]["shadow"]["1"][1]
        b.close(p2, 99.9, 3300.0, BLOCK, COST)                 # its paper add lost too: still held
        after_loss = dict(b.guard)
        reloaded = SetBlockBook(b.path)
        reloaded.cfg = b.cfg
        p3 = _pos(cid="p3", symbol="B-USDT")
        still = reloaded.candidate(p3, 100.12, 3400.0, BLOCK, COST, 3600.0, floor=1.1)
        reloaded.close(p3, 102.0, 3500.0, BLOCK, COST)         # the paper add made about 1.78 %: the deficit is earned back
        resumed = dict(reloaded.guard)
        p4 = _pos(cid="p4", symbol="B-USDT")
        sent = reloaded.candidate(p4, 100.12, 3600.0, BLOCK, COST, 3600.0, floor=1.1)
        off = _book(os.path.join(td, "x") if os.makedirs(os.path.join(td, "x"), exist_ok=True) is None else td, blockRelations=["symbol"],
                    blockCounts=[{"step": 1, "ratio": 1.0, "on": True}])
        _teach(off, _pos(cid="t3", symbol="B-USDT"), 101.5)
        off.guard = {"net": -0.5, "shadow": 0.0}
        no_guard = off.candidate(_pos(cid="q", symbol="B-USDT"), 100.12, 3700.0, BLOCK, COST, 3600.0, floor=1.1)
    ok = (deficit < -0.005 and held is None and held_again is None and abs(shadow_px - 100.12) < 1e-9 and after_loss["net"] == deficit and after_loss["shadow"] < 0
          and still is None and resumed == br.new_guard() and sent is not None and sent["group"] == 1 and no_guard is not None)
    return ok, f"deficit {deficit:.5f}; held {held is None}/{held_again is None}; after a losing paper add {after_loss}; after a winning one {resumed}; sent again {sent is not None}; guard off adds {no_guard is not None}"


def an_add_needs_room_to_the_target_and_an_allowed_parent_type():
    """blockTargetShare 0.5: a count of a fixed-target parent adds up to the middle of the way from the base entry to the target and not beyond; a parent
    that ignores its target (honor_tp off) is not bound by it. blockKinds ["base", "trail"]: an Exit-Set parent receives no add, its observations still
    collect; a trailing or base parent does. The Set type is read from the Set id for every id the grid builds."""
    import json
    import set_engine as se
    from common import PULSE
    with tempfile.TemporaryDirectory() as td:
        b = _book(td, blockTargetShare=0.5, blockCounts=[{"step": 1, "ratio": 1.0, "on": True}, {"step": 12, "ratio": 1.0, "on": True}])
        _teach(b, _pos(cid="t1", tp=103.0), 102.9, top=102.0)
        near = _pos(cid="a", tp=102.0)                         # the way to the target is 2 %: the middle is 101.0
        first = b.candidate(near, 100.12, 3000.0, BLOCK, COST, 3600.0, floor=1.1, honor_tp=True)
        _fill(near, first, 100.12)
        past = b.candidate(near, 101.35, 3001.0, BLOCK, COST, 3600.0, floor=1.1, honor_tp=True)        # count 2 at 1.3 %: past the middle
        free = b.candidate(near, 101.35, 3002.0, BLOCK, COST, 3600.0, floor=1.1, honor_tp=False)       # a parent without a fixed target
        far = _pos(cid="b", tp=103.0)                          # the way is 3 %: 1.3 % is inside its half
        _fill(far, b.candidate(far, 100.12, 3003.0, BLOCK, COST, 3600.0, floor=1.1, honor_tp=True), 100.12)
        inside = b.candidate(far, 101.35, 3004.0, BLOCK, COST, 3600.0, floor=1.1, honor_tp=True)
        k = _book(os.path.join(td, "k") if os.makedirs(os.path.join(td, "k"), exist_ok=True) is None else td, blockKinds=["base", "trail"], blockRelations=["symbol"],
                  blockCounts=[{"step": 1, "ratio": 1.0, "on": True}])
        _teach(k, _pos(cid="t2"), 101.5)
        kinds = {}
        for name, sid in (("base", "general:L:1m:sl1.00:st20"), ("exit", "general:L:1m:sl1.00:st20:xt"), ("trail", "general:L:1m:tr0.60:0.30")):
            kinds[name] = k.candidate(_pos(set_id=sid, cid="k" + name), 100.12, 3100.0, BLOCK, COST, 3600.0, floor=1.1) is not None
        x = _pos(set_id="general:L:1m:sl1.00:st20:xt", cid="kx")
        k.observe(x, 100.3, 3200.0, BLOCK, COST, 3600.0)
        k.close(x, 100.9, 3300.0, BLOCK, COST)
        exit_observed = k.ledgers["symbol|A-USDT|LONG"]["paper"]["1"][0] == 2
    book = se.SetBook()
    book.load(json.load(open(os.path.join(PULSE, "overlay-bingx-x01.json"))))
    ids_ok = all(br.kind_of_set(st.id) == st.kind for st in book.by_idx) and br.kind_of_set("trend:L:1m:sl1.00:st20") == "base"
    ok = (first is not None and past is None and free is not None and inside is not None and kinds == {"base": True, "exit": False, "trail": True}
          and exit_observed and ids_ok and br.clean_cfg({"blockKinds": ["base", "exit", "trail"]}).kinds == () and not br.clean_cfg({}).guard
          and br.clean_cfg({}).target_share == 0.0)
    return ok, f"past the middle {past is None}, without a fixed target {free is not None}, inside the half {inside is not None}; types {kinds}; exit parent observed {exit_observed}; ids {ids_ok}"


def the_simulator_and_the_study_apply_the_same_rules_as_the_engine():
    """block_rules is the one place the rules live: the engine book, scripts/stack_eval.block_sim2 and scripts/block_coordination_study.simulate call
    kind_allowed, share_allows, guard_holds and guard_after_close and none of them re-implements a rule. The guard arithmetic: a deficit holds, paper results
    earn it back and restart both at zero, a paper result without a deficit is not kept, a non-finite part changes nothing."""
    from common import ROOT
    src = {n: open(os.path.join(ROOT, p)).read() for n, p in (("engine", "server/pulse/set_block.py"), ("stack", "scripts/stack_eval.py"), ("study", "scripts/block_coordination_study.py"))}
    calls = {n: all(f"br.{fn}(" in text for fn in ("kind_allowed", "share_allows", "guard_holds", "guard_after_close")) for n, text in src.items()}
    cfg = br.clean_cfg({"blockGuard": True})
    g = br.new_guard()
    steps = []
    for ex, sh in ((0.4, 0.3), (-0.5, 0.0), (0.0, 0.05), (0.0, -0.2), (float("nan"), 0.1), (0.0, 0.2)):
        g = br.guard_after_close(cfg, g, ex, sh)
        steps.append((round(g["net"], 6), round(g["shadow"], 6), br.guard_holds(cfg, g)))
    want = [(0.4, 0.0, False), (-0.1, 0.0, True), (-0.1, 0.05, True), (-0.1, -0.15, True), (-0.1, -0.05, True), (0.0, 0.0, False)]
    return all(calls.values()) and steps == want and not br.guard_holds(br.clean_cfg({}), {"net": -1.0, "shadow": 0.0}), f"rule calls {calls}; guard walk {steps}"


def _naive_rounds(nets, n, ratio, step, max_level, pause, cap, others=0.0):
    """A plain reading of the owner's rule for ONE block count on positions that open and close one after another: returns each position's multiple."""
    level, deficit, rest, out, block = 0, 0.0, 0, [], []
    for net in nets:
        m = min(cap, 1.0 + ratio * level + others)
        out.append(m)
        block.append((net, m))
        if len(block) < n:
            continue
        unit = weighted = 0.0
        for x, w in block:                                   # added one by one, as a book adds each close (sum() rounds differently)
            unit += x
            weighted += x * w
        block = []
        if rest > 0:
            rest -= 1
        elif level == 0:
            if unit < 0:
                level, deficit = 1, weighted
            elif pause:
                rest = pause
        else:
            deficit += weighted
            if deficit >= 0:
                level, deficit, rest = 0, 0.0, pause
            elif step and unit < 0:
                level = min(max_level, level + 1)
    return out


def block_rounds_every_count_runs_its_own_procedure_by_the_owners_rule():
    """Block rounds (owner 2026-11-11): for every block count 1..6, ratio, step and pause the engine's book gives each position the multiple a plain reading
    of the rule gives (blocks of N, a losing block raises, kept until the weighted net is back at zero, step raises once more per further losing block, pause
    after a positive block or a recovery). Independence: with the other five counts observing (off) a count's multiples are exactly those of that count
    alone, and every count keeps its own blocks (count N settles len // N blocks); two groups of one relation never read each other; with all six on, the
    multiple is 1 + the sum of the six procedures' own ratio x level under the ceiling; overlapping positions decide from completed blocks only."""
    import random
    import block_rounds as rounds
    rng = random.Random(7)
    k = rounds.keys_of("A-USDT", 1, "general", "base", 5, "pos", "s1")
    k2 = rounds.keys_of("B-USDT", -1, "general", "base", 9, "pos", "s2")
    bad, cases = [], 0
    for n in rounds.COUNTS:
        for ratio, step, ml, pause in ((0.5, False, 1, 0), (1.0, True, 2, 0), (0.25, True, 6, 2), (2.0, True, 3, 1)):
            nets = [rng.choice((-1.0, -0.4, 0.3, 0.9, 1.5)) for _ in range(240)]
            want = _naive_rounds(nets, n, ratio, step, ml, pause, 5.0)
            for others_observe in (False, True):
                counts = {str(n): {"on": True, "ratio": ratio, "step": step, "maxLevel": ml, "pause": pause}}
                if others_observe:
                    counts.update({str(o): {"on": False, "ratio": 1.0, "step": True, "maxLevel": 6} for o in rounds.COUNTS if o != n})
                b = rounds.RoundsBook(cfg=rounds.config_of({"enabled": True, "relations": ["step"], "maxMult": 5, "counts": counts}))
                got = []
                for i, net in enumerate(nets):
                    m, _ = b.ask(k)
                    b.opened(f"p{i}", k, m)
                    b.opened(f"q{i}", k2, b.ask(k2)[0])          # another group of the same relation, always losing
                    b.closed(f"q{i}", -1.0)
                    b.closed(f"p{i}", net)
                    got.append(m)
                cases += 1
                if got != want:
                    bad.append((n, ratio, step, pause, others_observe, "multiples"))
                blocks = {o: b.groups[b.gid("step", k["step"], o)]["blocks"] for o in rounds.COUNTS}
                if blocks != {o: len(nets) // o for o in rounds.COUNTS}:
                    bad.append((n, "blocks", blocks))
    # all six on: the sum of the six procedures, each by its own reading of the same results (the multiple feeds every count's weighted net)
    nets = [rng.choice((-1.0, -0.4, 0.3, 0.9, 1.5)) for _ in range(300)]
    cfg = rounds.config_of({"enabled": True, "relations": ["step"], "maxMult": 4, "counts": {str(o): {"on": True, "ratio": 0.1 * o, "step": o % 2 == 0, "maxLevel": 3, "pause": o % 3} for o in rounds.COUNTS}})
    b = rounds.RoundsBook(cfg=cfg)
    summed = True
    for i, net in enumerate(nets):
        m, why = b.ask(k)
        lv = sum(cfg.counts[o].ratio * b.groups.get(b.gid("step", k["step"], o), {"level": 0})["level"] for o in rounds.COUNTS)
        summed = summed and abs(m - min(4.0, 1.0 + lv)) < 1e-12 and len(why) == sum(1 for o in rounds.COUNTS if b.groups.get(b.gid("step", k["step"], o), {"level": 0})["level"] > 0)
        b.opened(f"p{i}", k, m)
        b.closed(f"p{i}", net)
    # overlapping positions: three open at once, the block of 2 lost only when BOTH are closed; the third opened before that and is not raised
    o = rounds.RoundsBook(cfg=rounds.config_of({"enabled": True, "relations": ["step"], "maxMult": 5, "counts": {"2": {"on": True, "ratio": 1.0}}}))
    for pid in ("a", "b", "c"):
        o.opened(pid, k, o.ask(k)[0])
    o.closed("a", -1.0)
    early = o.ask(k)[0]
    o.closed("b", -1.0)
    late = o.ask(k)[0]
    ok = not bad and summed and early == 1.0 and late == 2.0 and o.members["c"]["mult"] == 1.0
    return ok, f"{cases} single-count books against the plain reading, mismatches {bad[:4]}; six counts sum {summed}; overlap before / after the block completed x{early} / x{late}"


def _replayed_rounds(history, n, c):
    """One procedure's (level, pause) recomputed from nothing: `history` is the group's positions in the order they opened, each
    {"mult", "net" (None = no result), "closed" (close sequence number or None)}. Blocks are the consecutive chunks of n; they settle in the order they
    were formed, a block only when every one of its positions has left; results are added in the order the positions closed."""
    level, deficit, rest = 0, 0.0, 0
    for i in range(0, len(history) - len(history) % n, n):
        block = history[i:i + n]
        if any(p["closed"] is None for p in block):
            break
        unit = weighted = 0.0
        scored = 0
        for p in sorted(block, key=lambda p: p["closed"]):
            if p["net"] is not None:
                unit += p["net"]
                weighted += p["net"] * p["mult"]
                scored += 1
        if not scored:
            continue
        if rest > 0:
            rest -= 1
        elif level == 0:
            if unit < 0:
                level, deficit = 1, weighted
            elif c["pause"]:
                rest = c["pause"]
        else:
            deficit += weighted
            if deficit >= 0:
                level, deficit, rest = 0, 0.0, c["pause"]
            elif c["step"] and unit < 0:
                level = min(c["maxLevel"], level + 1)
    return level, rest


def block_rounds_hold_step_pause_and_stack_by_the_rule_on_overlapping_positions():
    """Intensive: (1) worked cases in plain numbers - the ratio is always from the ORIGINAL volume (levels 1, 2, 3 give 1 + r, 1 + 2r, 1 + 3r, never
    compounded), step climbs one level per further losing block up to the top level and stops there, the raise is HELD over winning blocks that do not yet
    earn the deficit back, it ends on the block that brings the weighted net to zero or above (the boundary counts as recovered), a pause of P holds exactly P
    blocks at base volume (a losing block inside it starts nothing) and the first losing block after it raises again, a pause also follows a positive block
    at base volume. (2) 300 random books: up to 5 positions open at once, three relations, several groups, all six counts raising with their own random
    ratio / step / top level / pause, unscored closes and positions released without a close - every position's multiple equals 1 + the sum over the six
    counts of ratio x level, each level recomputed from nothing by a replay of the group's history; a released position never stalls its group; every
    count settles exactly the blocks whose positions have all left."""
    import random
    import block_rounds as rounds
    k = rounds.keys_of("A-USDT", 1, "general", "base", 5, "pos", "s1")

    def walk(counts, nets, cap=5.0):
        b = rounds.RoundsBook(cfg=rounds.config_of({"enabled": True, "relations": ["step"], "maxMult": cap, "counts": counts}))
        out = []
        for i, net in enumerate(nets):
            m = b.ask(k)[0]
            b.opened(f"p{i}", k, m)
            b.closed(f"p{i}", net)
            out.append(round(m, 6))
        return out, b

    worked = {}
    # count 1, ratio 0.5, step to 3: losses climb 1.5 / 2.0 / 2.5 and stay at the top; +1 at x 2.5 leaves the deficit (-1 -1.5 -2 -2.5 -2.5 +2.5 = -7): held
    worked["step"], _b = walk({"1": {"on": True, "ratio": 0.5, "step": True, "maxLevel": 3}}, [-1, -1, -1, -1, -1, 1, 1, 6, -1, -1])
    # after the 6 (x 2.5 = +15, net +10.5 with the +2.5 before) the group is recovered: base volume again, the next loss starts at level 1
    want_step = [1.0, 1.5, 2.0, 2.5, 2.5, 2.5, 2.5, 2.5, 1.0, 1.5]
    # no step: one level only, held through a win that does not recover (-1, then -1.5, +0.75 -> -1.75), ended exactly at zero by the boundary block
    worked["hold"], _b = walk({"1": {"on": True, "ratio": 0.5}}, [-1, -1, 0.5, -0.5, 1.0, 1.0, 0.5, -1])
    #   deficit: -1 | -2.5 | -1.75 | -2.5 | -1.0 | +0.5 recovered ... then base, a win at base, a loss -> next raised
    want_hold = [1.0, 1.5, 1.5, 1.5, 1.5, 1.5, 1.0, 1.0]
    worked["boundary"], bb = walk({"1": {"on": True, "ratio": 1.0}}, [-1, 0.5, -1])      # -1 then +0.5 x 2 = exactly 0: recovered
    want_boundary = [1.0, 2.0, 1.0]
    # pause 2 after a recovery: two blocks at base whatever they do (both lose: nothing starts), the third losing block raises
    worked["pause"], _b = walk({"1": {"on": True, "ratio": 1.0, "pause": 2}}, [-1, 2, -1, -1, -1, -1, 5, 1, -1, -1, -1])
    #   L0 lose -> L1 | x2 +4: recovered, pause 2 | paused | paused | lose -> L1 | x2 -2 held | x2 +10 recovered, pause 2 | paused (win) | paused | lose -> L1 | x 2
    want_pause = [1.0, 2.0, 1.0, 1.0, 1.0, 2.0, 2.0, 1.0, 1.0, 1.0, 2.0]
    # a pause also follows a positive block at base volume: win, then one paused block (a loss that starts nothing), then a loss that raises
    worked["pause after a win"], _b = walk({"1": {"on": True, "ratio": 1.0, "pause": 1}}, [1, -1, -1, -1])
    want_pw = [1.0, 1.0, 1.0, 2.0]
    # blocks of 3: nothing moves inside a block; all three of the next block are raised; step 2 after a second losing block
    worked["blocks of 3"], _b = walk({"3": {"on": True, "ratio": 0.25, "step": True, "maxLevel": 2}}, [-1, 1, -1, 1, -1, -1, 0, 0, 0, 9, 9, 9, 0])
    want_b3 = [1.0, 1.0, 1.0, 1.25, 1.25, 1.25, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.0]
    # two counts stack from the original volume and the ceiling binds: count 1 ratio 1.0 step to 4, count 2 ratio 0.5, ceiling x 3
    worked["stack"], _b = walk({"1": {"on": True, "ratio": 1.0, "step": True, "maxLevel": 4}, "2": {"on": True, "ratio": 0.5}}, [-1, -1, -1, -1], cap=3.0)
    want_stack = [1.0, 2.0, 3.0, 3.0]        # 1 | 1 + 1 | 1 + 2 + 0.5 capped at 3 | 1 + 3 + 0.5 capped
    plain_ok = (worked["step"] == want_step and worked["hold"] == want_hold and worked["boundary"] == want_boundary and worked["pause"] == want_pause
                and worked["pause after a win"] == want_pw and worked["blocks of 3"] == want_b3 and worked["stack"] == want_stack)

    rng = random.Random(20261111)
    bad, books, positions, raised, top = [], 0, 0, 0, 1.0
    for _case in range(300):
        counts = {str(n): {"on": rng.random() < 0.8, "ratio": rng.choice((0.1, 0.25, 0.5, 1.0, 2.0)), "step": rng.random() < 0.6,
                           "maxLevel": rng.randint(1, 6), "pause": rng.choice((0, 0, 1, 2, 6))} for n in rounds.COUNTS}
        rels = rng.sample(("overall", "symbol", "type", "set", "step", "direction"), 3)
        cap = rng.choice((2.0, 3.0, 5.0))
        cfg = rounds.config_of({"enabled": True, "relations": rels, "maxMult": cap, "counts": counts})
        b = rounds.RoundsBook(cfg=cfg)
        hist = {}                                # (relation, key) -> the group's positions in open order
        live, seq = [], 0
        for i in range(rng.randint(40, 160)):
            while live and (len(live) >= 5 or rng.random() < 0.45):          # close (or release) a random open position
                pid, rows = live.pop(rng.randrange(len(live)))
                seq += 1
                how = rng.random()
                net = None if how < 0.08 else rng.choice((-1.2, -0.7, -0.3, 0.2, 0.6, 1.4, 2.5))
                for row in rows:
                    row["closed"], row["net"] = seq, net
                if how < 0.04:
                    b.forget({p for p, _ in live})                           # left the book with no close record
                else:
                    b.closed(pid, net, scored=net is not None)
            keys = rounds.keys_of(rng.choice(("A", "B", "C")), rng.choice((1, -1)), rng.choice(("general", "indications")), rng.choice(("base", "trail")),
                                  rng.choice((5, 9)), "pos", rng.choice(("s1", "s2")))
            want = 1.0
            for rel in cfg.relations:
                h = hist.setdefault((rel, keys[rel]), [])
                for n in rounds.COUNTS:
                    c = counts[str(n)]
                    if c["on"]:
                        want += c["ratio"] * _replayed_rounds(h, n, c)[0]
            want = min(cap, want)
            got = b.ask(keys)[0]
            if abs(got - want) > 1e-9:
                bad.append((_case, i, round(got, 6), round(want, 6)))
                break
            b.opened(f"p{i}", keys, got)
            rows = []
            for rel in cfg.relations:
                row = {"mult": got, "net": None, "closed": None}
                hist[(rel, keys[rel])].append(row)
                rows.append(row)
            live.append((f"p{i}", rows))
            positions += 1
            raised += got > 1.0 + 1e-9
            top = max(top, got)
        for (rel, key), h in hist.items():       # every count settled exactly its complete blocks, in order
            for n in rounds.COUNTS:
                g = b.groups[b.gid(rel, key, n)]
                done = 0
                for j in range(0, len(h) - len(h) % n, n):
                    if any(p["closed"] is None for p in h[j:j + n]):
                        break
                    done += 1
                pending = sum(1 for _ in range(done * n, len(h), n))
                if len(g["open"]) != pending or (g["level"], g["pause"]) != _replayed_rounds(h, n, counts[str(n)]):
                    bad.append((_case, rel, key, n, "state"))
        books += 1
    ok = plain_ok and not bad and raised > 1000 and top >= 5.0 - 1e-9
    return ok, (f"worked cases {worked if not plain_ok else 'all equal the hand calculation'}; {books} random books, {positions} positions ({raised} raised, "
                f"largest multiple x{top:.2f}), mismatches {bad[:3]}")


def block_rounds_ship_the_procedure_measured_at_or_above_without():
    """The shipped overlays, the three presets that carry Block and the desk default hold the same blockRounds object, and it is the measured one: relation
    step, block count 5 raising (ratio 1.0, step to level 2, no pause), the other five counts forming their blocks without raising, ceiling x 3. The engine's
    absent-key default is off (fixtures and older overlays raise nothing)."""
    import json
    import re
    import block_rounds as rounds
    from common import ROOT
    pulse = os.path.join(ROOT, "server", "pulse")
    files = [os.path.join(pulse, f) for f in sorted(os.listdir(pulse)) if f.startswith("overlay-") and f.endswith(".json")]
    files += [os.path.join(ROOT, "presets", f) for f in ("secure-minimal.json", "secure-minimal-expanded-research.json", "low-steps-coordination-research.json")]
    carried = {os.path.basename(f): json.load(open(f)).get("blockRounds") for f in files}
    want = rounds.config_of({"enabled": True, "relations": ["step"], "maxMult": 3, "counts": {"5": {"on": True, "ratio": 1.0, "step": True, "maxLevel": 2, "pause": 0}}}).to_json()
    same = {name: (isinstance(v, dict) and rounds.config_of(v).to_json() == want and v == want) for name, v in carried.items()}
    desk = open(os.path.join(ROOT, "src", "lib", "block-rounds.ts")).read()
    desk_ok = bool(re.search(r'enabled: true,\s*relations: \["step"\],\s*maxMult: 3,', desk)) and '"5": { on: true, ratio: 1, step: true, maxLevel: 2, pause: 0 }' in desk
    off = rounds.config_of(None)
    return all(same.values()) and len(same) == 7 and desk_ok and not off.enabled and not any(c.on for c in off.counts.values()), f"carried equal to the measured object: {same}; desk {desk_ok}; absent key enabled {off.enabled}"


CHECKS = [
    ("block_structure.every-count-is-independent-and-stacks-from-the-original-volume", every_count_is_independent_and_stacks_from_the_original_volume),
    ("block_structure.six-counts-one-step-apart-run-six-independent-procedures", six_counts_one_step_apart_run_six_independent_procedures),
    ("block_structure.evidence-is-kept-per-set-and-overall-for-the-open-parents", evidence_is_kept_per_set_and_overall_for_the_open_parents),
    ("block_structure.the-deficit-guard-holds-adds-until-paper-adds-earn-the-deficit-back", the_deficit_guard_holds_adds_until_paper_adds_earn_the_deficit_back),
    ("block_structure.an-add-needs-room-to-the-target-and-an-allowed-parent-type", an_add_needs_room_to_the_target_and_an_allowed_parent_type),
    ("block_structure.the-simulator-and-the-study-apply-the-same-rules-as-the-engine", the_simulator_and_the_study_apply_the_same_rules_as_the_engine),
    ("block_structure.block-rounds-every-count-runs-its-own-procedure-by-the-owners-rule", block_rounds_every_count_runs_its_own_procedure_by_the_owners_rule),
    ("block_structure.block-rounds-hold-step-pause-and-stack-by-the-rule-on-overlapping-positions", block_rounds_hold_step_pause_and_stack_by_the_rule_on_overlapping_positions),
    ("block_structure.block-rounds-ship-the-procedure-measured-at-or-above-without", block_rounds_ship_the_procedure_measured_at_or_above_without),
]
