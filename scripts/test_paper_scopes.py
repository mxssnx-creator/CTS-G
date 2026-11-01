"""Independent scope/cutoff oracle and bounded-work benchmark; no venue access."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import random
import resource
import statistics
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CTSG_PULSE_DIR", str(ROOT / "server" / "pulse")))
import set_engine as se


def make_book(count=12):
    book = se.SetBook()
    book.lookback = 120
    book.packs = ["general", "signals-fixture", "shortrange"]
    book.by_idx = [se.SetState(id=f"scope-{i}", pack=book.packs[i % 3], tf="1m", sl_ratio=1.0,
                              trail_key="", trail_arm=0.0, trail_give=0.0, idx=i)
                   for i in range(count)]
    book.sets = {st.id: st for st in book.by_idx}
    return book


def reference(book, n, scoped=True):
    """Full scan/sort, no engine scope implementation, heap, or prefix sum."""
    n = max(1, int(n))
    anchor = max((st.score_at for st in book.by_idx), default=0.0)
    cutoff = anchor - book._evidence_lookback() * se.BAR_S if anchor > 0 else -math.inf
    upper = anchor if anchor > 0 else math.inf
    groups = {"overall": []}
    if scoped:
        groups.update({"lane:pos": [], "lane:neg": []})
        for pack in book.packs:
            groups.update({f"pack:{pack}": [], f"pack:{pack}:pos": [], f"pack:{pack}:neg": []})
    for si, st in enumerate(book.by_idx):
        limit = min(upper, st.score_at) if st.score_at > 0 else upper
        for li, (lane, tape) in enumerate((("pos", st.exe), ("neg", st.neg))):
            for ri, (t, value) in enumerate(tape):
                if (t < cutoff or t > limit or (st.score_before and st.score_at > 0 and t == limit)):
                    continue
                item = (t, (si, li, ri), value, lane)
                keys = ["overall"]
                if scoped:
                    keys += [f"lane:{lane}", f"pack:{st.pack}", f"pack:{st.pack}:{lane}"]
                for key in keys:
                    groups.setdefault(key, []).append(item)
    cf = book.cost_pct / 100.0
    return {key: [{"t": t, "pnl_pct": value + cf, "pnl": value, "lane": lane}
                  for t, _order, value, lane in sorted(rows, key=lambda r: r[:2])[-n:]]
            for key, rows in groups.items()}


class CountedTape(list):
    reads = 0

    def __getitem__(self, key):
        self.reads += len(range(*key.indices(len(self)))) if isinstance(key, slice) else 1
        return super().__getitem__(key)


def benchmark():
    book = make_book(1654)
    anchor = 1_700_000_000.0
    for i, st in enumerate(book.by_idx):
        st.score_at, st.score_before = anchor, True
        st.exe = CountedTape((anchor - (200 - j) * 60 - (i % 5), .004 if j % 3 else -.003) for j in range(200))
        st.neg = CountedTape((anchor - (200 - j) * 60 - (i % 7), -.002 if j % 4 else .005) for j in range(200))
    durations = []
    for _ in range(5):
        for st in book.by_idx:
            st.exe.reads = st.neg.reads = 0
        started = time.perf_counter()
        result = book.real_paper_scopes(200)
        durations.append(1000 * (time.perf_counter() - started))
    reads = sum(st.exe.reads + st.neg.reads for st in book.by_idx)
    return {"scope": "synthetic sorted paper tapes; not strategy profitability or live loop latency",
            "sets": len(book.by_idx), "storedRows": 1654 * 400, "lastN": 200,
            "scopes": len(result), "outputRows": sum(map(len, result.values())),
            "matchesIndependentOracle": result == reference(book, 200),
            "outputSha256": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest(),
            "engineSha256": hashlib.sha256(Path(se.__file__).read_bytes()).hexdigest(),
            "tapeItemsRead": reads, "milliseconds": durations,
            "medianMilliseconds": statistics.median(durations), "maxMilliseconds": max(durations),
            "peakRssKiB": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


class PaperScopes(unittest.TestCase):
    def test_decision_clock_excludes_equal_close_but_retrospective_includes_it(self):
        book = make_book(1)
        st = book.by_idx[0]
        st.score_at, st.score_before = 1000.0, True
        st.exe = [(999.0, .002), (1000.0, -.005), (1001.0, .01)]
        got = book.real_paper_scopes(10)
        self.assertEqual(got, reference(book, 10))
        self.assertEqual([r["t"] for r in got["overall"]], [999.0])
        st.score_before = False
        self.assertEqual([r["t"] for r in book.real_paper_scopes(10)["overall"]], [999.0, 1000.0])

    def test_another_set_clock_does_not_release_unscored_rows(self):
        book = make_book(2)
        a, b = book.by_idx
        a.score_at, a.score_before = 1000.0, True
        a.exe = [(999.0, -.01), (1000.0, .2), (1001.0, .5)]
        b.score_at = 1100.0
        self.assertEqual(book.real_paper_scopes(10), reference(book, 10))

    def test_window_selects_latest_eligible_not_latest_unfiltered_rows(self):
        book = make_book(1)
        st = book.by_idx[0]
        st.score_at, st.score_before = 1000.0, True
        st.exe = [(float(t), .01) for t in range(990, 1010)]
        self.assertEqual(book.real_paper_scopes(3), reference(book, 3))
        self.assertEqual([r["t"] for r in book.real_paper(3)], [997.0, 998.0, 999.0])

    def test_pack_and_direction_scopes_do_not_mix(self):
        book = make_book(3)
        for st in book.by_idx:
            st.score_at = 1000.0
        book.by_idx[0].exe = [(999.0, .01)]
        before = book.real_paper_scopes(10)
        book.by_idx[1].neg = [(999.0, -.9)]
        after = book.real_paper_scopes(10)
        self.assertEqual(before["pack:general:pos"], after["pack:general:pos"])
        self.assertNotEqual(before["overall"], after["overall"])
        self.assertEqual(after["pack:signals-fixture:neg"][0]["pnl"], -.9)

    def test_matches_full_scan_with_ties_clocks_windows_and_empty_lanes(self):
        rng = random.Random(812)
        for seed in range(12):
            book = make_book(12)
            for st in book.by_idx:
                st.score_at = rng.choice((0.0, 1000.0, 1010.0))
                st.score_before = rng.choice((True, False))
                for lane in ("exe", "neg"):
                    setattr(st, lane, sorted([(float(rng.randrange(-7000, 1020, 10)), rng.choice((-.003, .0, .002)))
                                             for _ in range(rng.randrange(0, 250))], key=lambda r: r[0]))
            for n in (1, 3, 15, 200):
                with self.subTest(seed=seed, n=n):
                    self.assertEqual(book.real_paper_scopes(n), reference(book, n))
                    self.assertEqual(book.real_paper(n), reference(book, n, scoped=False)["overall"])

    def test_top_n_work_does_not_scan_all_stored_rows(self):
        got = benchmark()
        self.assertTrue(got["matchesIndependentOracle"])
        self.assertLess(got["tapeItemsRead"], got["storedRows"] // 4, got)


if __name__ == "__main__":
    if "--benchmark" in sys.argv:
        parser = argparse.ArgumentParser()
        parser.add_argument("--benchmark", action="store_true")
        parser.add_argument("--out", type=Path)
        args = parser.parse_args()
        result = benchmark()
        rendered = json.dumps(result, indent=2) + "\n"
        if args.out:
            args.out.write_text(rendered)
        print(rendered)
    else:
        unittest.main(verbosity=2)
