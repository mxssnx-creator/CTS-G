"""Indications by range (indicationConfigs) and the distinct-tape statistic.

The expansion is read against a plain re-derivation of the names, the Sets it builds against the grid rules (steps, ratios, types, trailing arm cap),
and the pooled distinct-tape figures against a plain loop over the Sets."""
from __future__ import annotations

from common import T0, overlay
import indication_config as ic
import indication_extra as ie
import set_engine as se

SNAP = lambda x: min(2.5, max(0.25, round(x * 4) / 4))      # noqa: E731  (the 0.25 grid of SL ratios, 0.25-2.5)
RATIOS = [0.5, 1.0, 1.5]


def _expand(configs):
    return ic.expand(configs, ie.known, 2, 30, SNAP, RATIOS)


def expansion_is_the_cross_product_of_known_names():
    out = _expand([{"label": "t", "families": ["donch", "kelt"], "ranges": [8, 14], "timeframes": [1, 5, "c"], "relations": ["", "T5"], "stepMin": 3, "stepMax": 9}])
    want = set()
    for fam in ("donch", "kelt"):
        for r in (8, 14):
            for suf in (f"_n{r}_t1", f"_n{r}_t5", f"_c{r}"):
                for rel in ("", "@T5"):
                    name = fam + suf + rel
                    if ie.known(name):
                        want.add(name)
    got = {ic.source_name(n) for n in out["names"]}
    rng = out["ranges"][out["names"][0]]
    ok = got == want and len(got) >= 12 and rng["lo"] == 3 and rng["hi"] == 9 and not [d for d in out["dropped"] if d["reason"] == "over the bound"]
    return ok, f"{len(got)} names == plain cross product ({len(want)}); step range {rng['lo']}-{rng['hi']}; dropped={len(out['dropped'])}"


def unknown_and_over_bound_combinations_are_dropped_and_listed():
    out = _expand([{"families": ["donch", "nosuchfamily"], "ranges": [8], "timeframes": [5], "relations": ["", "zz9"]}])
    reasons = {(d["name"], d["reason"]) for d in out["dropped"]}
    wide = _expand([{"families": ["donch", "kelt", "brk", "atrx", "adx", "roc"], "ranges": [5, 8, 14, 21], "timeframes": [1, 5, 15, 30], "relations": ["", "T5", "B15"]}])
    cap_ok = len(wide["names"]) <= ic.MAX_NAMES_PER_ENTRY and any(d["reason"] == "over the bound" for d in wide["dropped"])
    ok = [ic.source_name(n) for n in out["names"]] == ["donch_n8_t5"] and ("nosuchfamily_n8_t5", "unknown to the library") in reasons and ("donch_n8_t5@zz9", "unknown to the library") in reasons and cap_ok
    return ok, f"kept={out['names']} dropped={len(out['dropped'])}; wide entry capped at {len(wide['names'])} (<= {ic.MAX_NAMES_PER_ENTRY}) with {len(wide['dropped'])} dropped"


def settings_are_normalised_and_pause_semantics_hold():
    out = _expand([
        {"names": ["g_trend_min"], "stepMin": 99, "stepMax": 1, "slRatios": [0.6, "x", 9], "types": ["exit", "nope"], "pause": 0},
        {"names": ["g_revert_min"], "pause": 25},
        {"names": ["g_volume_min"], "pause": "auto", "enabled": True},
        {"names": ["g_all_min"], "enabled": False},
    ])
    out["ranges"] = {ic.source_name(n): v for n, v in out["ranges"].items()}
    out["pause"] = {ic.source_name(n): v for n, v in out["pause"].items()}
    out["names"] = [ic.source_name(n) for n in out["names"]]
    a, b_, c = (out["ranges"][n] for n in ("g_trend_min", "g_revert_min", "g_volume_min"))
    ok = (out["names"] == ["g_revert_min", "g_trend_min", "g_volume_min"]
          and (a["lo"], a["hi"]) == (30, 30) and a["kinds"] == ["exit"] and a["ratios"] == [0.5, 2.5]
          and out["pause"] == {"g_trend_min": 0, "g_revert_min": 25} and "g_volume_min" not in out["pause"]
          and b_["kinds"] == list(ic.KINDS) and c["lo"] == 2 and c["ratios"] == RATIOS)
    return ok, f"steps clamped {a['lo']}-{a['hi']}, ratios {a['ratios']}, types {a['kinds']}, pause map {out['pause']} (auto absent, disabled entry skipped)"


def sets_follow_the_configured_ranges():
    b = se.SetBook()
    b.load(overlay(stratIndications=False, stratGeneral=False, stratShortRange=False, indicationConfigs=[
        {"families": ["donch"], "ranges": [8], "timeframes": [5], "stepMin": 3, "stepMax": 6, "slRatios": [1.0, 2.0], "types": ["base", "exit", "trail"], "pause": 20},
        {"names": ["g_trend_min"], "stepMin": 4, "stepMax": 4, "slRatios": [1.5], "types": ["exit"], "pause": 0}]))
    donch = [s for s in b.by_idx if ic.source_name(s.pack) == "extra.donch_n8_t5"]
    trend = [s for s in b.by_idx if ic.source_name(s.pack) == "extra.g_trend_min"]
    base = [s for s in donch if s.kind == "base"]
    trail = [s for s in donch if s.kind == "trail"]
    arm_cap = 6 * b.cost_pct + 1e-9
    ok = ([ic.source_name(p) for p in b.packs] == ["extra.donch_n8_t5", "extra.g_trend_min"]
          and len(base) == 2 * 4 and {s.step for s in base} == {3, 4, 5, 6} and {s.sl_ratio for s in base} == {1.0, 2.0}
          and trail and all(s.trail_arm <= arm_cap for s in trail)
          and len(trend) == 1 and trend[0].kind == "exit" and trend[0].step == 4
          and b.pause_minutes(donch[0].pack, "A") == 20 and not b.pause_active(trend[0].pack) and b.pause_active(donch[0].pack))
    return ok, f"donch base={len(base)} exit={sum(1 for s in donch if s.kind == 'exit')} trail={len(trail)} (arms <= largest step); trend sets={len(trend)}; pause donch 20, trend none"


def configured_indications_replay_and_score_end_to_end():
    b = se.SetBook()
    b.load(overlay(stratIndications=False, stratGeneral=False, stratShortRange=False, pauseMode="auto", pauseAutoMin=8, indicationConfigs=[
        {"names": ["g_trend_min", "g_revert_min"], "stepMin": 3, "stepMax": 5, "slRatios": [1.0], "types": ["base", "exit"]}]))
    from common import synth_bars
    for i in range(3):
        b.bars[f"S{i}-USDT"] = synth_bars(40 + i, 900, drift=0.0004 if i % 2 == 0 else -0.0004)
    b.replay_all(now=T0)
    p = b.progress
    stuck = [s.id for s in b.by_idx if s.last_error]
    rep = b._pause_report()
    ok = p.phase == "ready" and p.errors == 0 and not stuck and len(b.by_idx) == 2 * (3 + 3) and set(rep["chosen"]) <= set(b.packs)
    return ok, f"phase={p.phase} sets={len(b.by_idx)} errors={p.errors} stuck={len(stuck)}; pause choices for {sorted(rep['chosen'])}"


def distinct_tape_pools_count_each_tape_once():
    b = se.SetBook()
    b.load(overlay(stratIndications=False, stratShortRange=False, setGateMinTrades=0, setMinPf=1.0, stageFullWindow=False))
    from common import synth_bars
    for i in range(3):
        b.bars[f"S{i}-USDT"] = synth_bars(60 + i, 900, vol=0.0006, drift=0.0003)       # quiet bars: wide trailing arms are never reached
    b.replay_all(now=T0)
    # validity follows the tape in the engine (a copy of a valid Set is valid); no Set validates on quiet random bars, so give half of the tapes a verdict
    # of "valid" to exercise the pooled arithmetic itself
    verdict = lambda st: "" if st.tape_sig and st.tape_sig % 2 == 0 else "pf<min"      # noqa: E731
    b.validation_reason = verdict
    b.base_validation_reason = verdict
    pf = b._pf_split()
    d = pf["distinct"]
    traded = [s for s in b.by_idx if s.tape_sig]
    groups = {}
    for s in traded:
        groups.setdefault(s.tape_sig, []).append(s)
    # plain reading: one Set per tape (the first in grid order), pooled by a loop
    first = {sig: g[0] for sig, g in groups.items()}
    valid_ids = {s.id for s in b.by_idx if not b.validation_reason(s)}
    canon = [s for s in first.values() if s.id in valid_ids]
    # a copy of a valid Set is valid or not on the same tape: every member of a group agrees
    agree = all(len({s.id in valid_ids for s in g}) == 1 for g in groups.values())
    gp, gl = sum(s.gate_gp for s in canon), sum(s.gate_gl for s in canon)
    want = round(gp / gl, 4) if gl > 0 else (99.0 if gp > 0 else 0.0)
    ok = (d["traded"] == len(traded) and d["tapes"] == len(groups) and d["copies"] == len(traded) - len(groups)
          and d["largestGroup"] == max(len(g) for g in groups.values()) and d["copies"] > 0 and agree
          and pf["baseDistinct"]["sets"] == len(canon) and pf["baseDistinct"]["pf"] == want and 0 < pf["baseDistinct"]["sets"] < pf["base"]["sets"]
          and pf["baseOnlyDistinct"]["sets"] == len(canon))
    return ok, f"traded={d['traded']} tapes={d['tapes']} copies={d['copies']} largest group={d['largestGroup']}; valid {pf['base']['sets']} -> distinct {pf['baseDistinct']['sets']}, PF {pf['base']['pf']} -> {pf['baseDistinct']['pf']} (plain loop {want})"


CHECKS = [("ranges." + fn.__name__.replace("_", "-"), fn) for fn in (
    expansion_is_the_cross_product_of_known_names, unknown_and_over_bound_combinations_are_dropped_and_listed,
    settings_are_normalised_and_pause_semantics_hold, sets_follow_the_configured_ranges,
    configured_indications_replay_and_score_end_to_end, distinct_tape_pools_count_each_tape_once)]
