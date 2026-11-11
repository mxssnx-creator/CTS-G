"""Golden suite: a seeded replay is fingerprinted, so any change in processing shows up as a diff.

The fingerprint covers every trade row of every Set on fixed synthetic bars. If a processing change is
intended, run  python3 scripts/regression/run.py --update-golden  and commit the new golden.json.
"""
from __future__ import annotations

import hashlib
import json
import os

from common import HERE, T0, overlay, synth_bars
from set_engine import SetBook

GOLDEN = os.path.join(HERE, "golden.json")


def _replay_fingerprint(**extra):
    b = SetBook()
    b.load(overlay(**extra))
    for i in range(3):
        b.bars[f"G{i}-USDT"] = synth_bars(101 + i, 1200, drift=0.0005 if i else -0.0005)
    hist = {st.id: [] for st in b.by_idx}
    for sym in sorted(b.bars):
        b._replay_symbol(sym, hist, T0, None)
    h = hashlib.sha256()
    rows = 0
    for st in b.by_idx:
        for r in hist[st.id]:
            rows += 1
            h.update(repr((st.id, r["symbol"], round(r["t"], 6), r["reason"], round(r["pnl"], 12),
                           round(r["pnl_pct"], 12), r["side"])).encode())
    return {"sets": len(b.by_idx), "trades": rows, "sha256": h.hexdigest()}


def fingerprint_matches_golden():
    got = _replay_fingerprint()
    got_auto = _replay_fingerprint(pauseMode="auto")
    got_default = _replay_fingerprint(pauseMode="auto", directionMode="split")        # the shipped defaults: a Set per direction, the pause on auto
    if os.environ.get("CTSG_UPDATE_GOLDEN") == "1":
        json.dump({**got, "pauseAuto": got_auto, "splitAuto": got_default}, open(GOLDEN, "w"), indent=1)
        return True, f"golden updated: sets={got['sets']} trades={got['trades']} (pause auto {got_auto['trades']}; split + auto {got_default['sets']} Sets {got_default['trades']})"
    if not os.path.exists(GOLDEN):
        return False, "golden.json missing: run  python3 scripts/regression/run.py --update-golden"
    want = json.load(open(GOLDEN))
    want_auto = want.pop("pauseAuto", None)
    want_default = want.pop("splitAuto", None)
    ok = got == want and got_auto == want_auto and got_default == want_default
    return ok, (f"sets={got['sets']} trades={got['trades']} sha={got['sha256'][:12]}; pause auto trades={got_auto['trades']} sha={got_auto['sha256'][:12]}; "
                f"split + auto sets={got_default['sets']} trades={got_default['trades']} sha={got_default['sha256'][:12]}") + (
        "" if ok else " != golden: processing changed; update the golden if intended")


CHECKS = [("golden.replay-fingerprint", fingerprint_matches_golden)]
