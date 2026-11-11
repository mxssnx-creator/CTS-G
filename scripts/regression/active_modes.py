"""Block's Active option (skip to the live count) and the Normal / Trailing switches: what they change and what they must not.

Active: an extra row, kind "active-live", for the group whose number is the live position count (the earlier groups are skipped), emitted before the
regular groups. It is a COPY of that group's own row: the group's PF decision, its pause and its target still decide, so Active cannot add into a
group that is not proven, and it adds one partial, never a jump. Normal off: the base Sets keep being replayed and scored (their statistics and the
Exit / trailing / additional strategies that read them stay), but no entry is ever taken under them."""
from __future__ import annotations

from common import SMALL, T0, overlay, synth_bars
from lanes import _block
import set_engine as se


def _lane(**cfg):
    b = _block(**cfg)
    lane = b.register_parent("X-USDT", "LONG", 10.0, 100.0)
    for n in range(1, int(b.max_stack) + 1):
        lane.pf_ring[n] = [3.0] * 8
    return b, lane


def active_row_skips_to_the_live_count_and_copies_that_groups_own_row():
    b, lane = _lane()
    out = {}
    for live in (0, 1, 3, 6, 9):
        rows = b.evaluate_counts(lane, live_n=live, intern_pf=1.5)
        act = [r for r in rows if r["kind"] == "active-live"]
        reg = {r["blockCount"]: r for r in rows if r["kind"] == "regular"}
        out[live] = (len(act), act[0]["blockCount"] if act else None)
        if act:
            a, g = act[0], reg[act[0]["blockCount"]]
            same = all(a[k] == g[k] for k in ("requestedAddQty", "passesProfitFactor", "paused", "targetSatisfied", "targetAddQty"))
            out[live] += (same, a["setKey"].endswith(f"#block:active:{a['blockCount']}"))
    off_live = _block(blockActiveLiveEnabled=False)
    off_real = _block(blockActiveRealEnabled=False)
    nothing = []
    for bb in (off_live, off_real):
        ln = bb.register_parent("X-USDT", "LONG", 10.0, 100.0)
        for n in range(1, 7):
            ln.pf_ring[n] = [3.0] * 8
        nothing.append(sum(r["kind"] == "active-live" for r in bb.evaluate_counts(ln, live_n=3, intern_pf=1.5)))
    want = {0: (0, None), 1: (1, 1, True, True), 3: (1, 3, True, True), 6: (1, 6, True, True), 9: (1, 6, True, True)}      # 9 live positions: capped at the 6th group
    return out == want and nothing == [0, 0], f"active row by live count {out}; with either Active switch off: {nothing} rows"


def active_is_emitted_first_adds_one_partial_and_never_exceeds_the_stack():
    b, lane = _lane()
    first = b.pick_emit(b.evaluate_counts(lane, live_n=3, intern_pf=1.5))
    sizes, kinds = [], []
    for _ in range(30):
        row = b.pick_emit(b.evaluate_counts(lane, live_n=3, intern_pf=1.5))
        if not row:
            break
        sizes.append(round(row["requestedAddQty"], 9))
        kinds.append(row["kind"])
        b.record_fill(lane, row, row["requestedAddQty"], f"c{len(sizes)}", f"o{len(sizes)}")
    total = round(lane.base_qty + lane.confirmed_add, 6)
    ok = first["kind"] == "active-live" and first["blockCount"] == 3 and all(x <= 5.0 + 1e-9 for x in sizes) and total <= 40.0 + 1e-9 and len(sizes) <= 6
    return ok, f"first emission is the {first['kind']} row of group {first['blockCount']}; orders {sizes} (a partial is 5), final volume {total} <= 40 (V0 x (1 + 6 x 0.5))"


def active_never_adds_into_a_group_that_is_not_proven_or_is_paused():
    b, lane = _lane()
    lane.pf_ring[3] = [-1.0] * 8                                  # group 3 loses: its own PF decision fails
    rows = b.evaluate_counts(lane, live_n=3, intern_pf=1.5)
    act = next(r for r in rows if r["kind"] == "active-live")
    picked = b.pick_emit(rows)
    b2, lane2 = _lane()
    lane2.pause_remaining[3] = 2                                  # group 3 is paused
    rows2 = b2.evaluate_counts(lane2, live_n=3, intern_pf=1.5)
    act2 = next(r for r in rows2 if r["kind"] == "active-live")
    picked2 = b2.pick_emit(rows2)
    ok = (not act["passesProfitFactor"] and act["requestedAddQty"] == 0 and picked is not None and picked["kind"] == "regular" and picked["blockCount"] != 3
          and act2["paused"] and act2["requestedAddQty"] == 0 and picked2["blockCount"] != 3)
    return ok, f"unproven group 3: active row passes={act['passesProfitFactor']} asks {act['requestedAddQty']}, the book emits group {picked['blockCount']} instead; paused group 3: asks {act2['requestedAddQty']}, emits group {picked2['blockCount']}"


def normal_off_keeps_processing_the_base_sets_in_both_directions_and_never_executes_them():
    out = {}
    for mode in ("both", "split"):
        b = se.SetBook()
        b.load(overlay(**{**SMALL, "setStepMax": 5, "stratIndications": False, "stratShortRange": False, "stratGeneral": True,
                          "stratNormal": False, "directionMode": mode, "pauseMode": "off", "minPfByStep": False, "setMinPf": 1.05,
                          "stagesEnabled": False, "mainGateEnabled": False,
                          "blockEnabled": False, "dcaEnabled": False,
                          **{"axis" + name + "Enabled": False for name in ("Prev", "Last", "Cont", "Pause")}}))
        b.bars["S0-USDT"] = synth_bars(81, 900, vol=0.004)
        b.replay_all(now=T0)
        base = [s for s in b.by_idx if s.kind == "base"]
        other = [s for s in b.by_idx if s.kind != "base"]
        scored = sum(1 for s in base if s.n > 0)
        # force every Set to look perfect: still, no base Set may be eligible or picked
        for s in b.by_idx:
            s.active, s.locked, s.last_error, s.gate_n, s.gate_pf, s.max_dd_s = True, False, "", 50, 3.0, 0.0
        eligible_base = sum(1 for s in base if b.is_eligible(s))
        picks = {side: b.pick_any("general", side=side) for side in ((1, -1) if mode == "split" else (0,))}
        out[mode] = (len(base), scored, eligible_base, all(p is None or p.kind != "base" for p in picks.values()),
                     all(b.gate_reason(s) == "type-disabled" for s in base), any(b.is_eligible(s) for s in other) and not any(b.gate_reason(s) == "type-disabled" for s in other))
    ok = all(v[0] > 0 and v[1] > 0 and v[2] == 0 and v[3] and v[4] and v[5] for v in out.values())
    return ok, f"(base Sets, scored, eligible, never picked, reason type-disabled, other kinds still eligible) {out}"


CHECKS = [("active." + fn.__name__.replace("_", "-"), fn) for fn in (
    active_row_skips_to_the_live_count_and_copies_that_groups_own_row, active_is_emitted_first_adds_one_partial_and_never_exceeds_the_stack,
    active_never_adds_into_a_group_that_is_not_proven_or_is_paused, normal_off_keeps_processing_the_base_sets_in_both_directions_and_never_executes_them)]
