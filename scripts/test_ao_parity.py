"""The hourly lane's A-O port reproduces CTS-A-O exactly.

Golden data comes from A-O's own TypeScript (scripts/ao_golden.mjs) on real
BingX 1h bars plus a copy with gaps. Every state, 4h bar, entry signal,
filter and simulated trade must match bit for bit.
"""
import json
import math
import os
import sys
import unittest

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server", "pulse"))

import ao_indications as AI  # noqa: E402
import ao_sim as AS  # noqa: E402

FIX = os.path.join(ROOT, "scripts", "fixtures")
with open(os.path.join(FIX, "ao_golden.json")) as f:
    GOLDEN = json.load(f)
with open(os.path.join(FIX, "ao_bars_1h.json")) as f:
    RAW = json.load(f)


def enc(a):
    return "".join("+" if x > 0 else "-" if x < 0 else "0" for x in a)


def cache(sym, candles=None):
    return AI.SeriesCache(AI.bars_from_candles(sym, 60, candles if candles is not None else RAW[sym]))


class ParityTests(unittest.TestCase):
    def test_pairs_and_cells(self):
        self.assertEqual(list(AI.PAIRS_ALL), GOLDEN["pairs"])
        self.assertEqual(AS.RT_COST, GOLDEN["cost"])
        got = [(c.tp, c.sl, c.trail, c.hold, c.trail_step, c.trail_free) for c in AS.grid_cells(GOLDEN["grid"])]
        exp = [(c["tp"], c["sl"], c["trail"], c["hold"], c["trailStep"], c["trailFree"]) for c in GOLDEN["cells"]]
        self.assertEqual(got, exp)

    def test_states_htf_signals_filters(self):
        for sym, g in GOLDEN["symbols"].items():
            k = cache(sym)
            for ind, st in g["states"].items():
                self.assertEqual(enc(AI.indication_state(ind, k)), st, f"{sym} {ind}")
            hk, mp = k.htf(4)
            self.assertEqual(mp.tolist(), g["htf"]["map"], sym)
            for col in "tohlcv":
                self.assertEqual(getattr(hk.b, col).tolist(), g["htf"][col], f"{sym} htf {col}")
            for pair, s in g["signals"].items():
                self.assertEqual(enc(AI.pair_signal(pair, k)), s, f"{sym} {pair}")
            alt = np.array([1 if i % 2 else -1 for i in range(k.b.n)], dtype=np.int8)
            for name, s in g["filters"].items():
                self.assertEqual(enc(AI.apply_filter(name, alt, k)), s, f"{sym} {name}")

    def test_series(self):
        worst = 0.0
        for sym, g in GOLDEN["symbols"].items():
            k = cache(sym)
            bb20, bb50, dmi, mac, kc = k.bb(20, 2), k.bb(50, 2), k.dmi(14), k.macd(), k.kelt(20, 2.5)
            ours = {
                "rsi10": k.rsi(10), "rsi14": k.rsi(14), "rsi21": k.rsi(21), "rsi7": k.rsi(7), "ema20": k.ema(20),
                "ema50": k.ema(50), "atr14": k.atr(14), "bb20up": bb20["up"], "bb20lo": bb20["lo"],
                "bb20width": bb20["width"], "bb50up": bb50["up"], "cci14": k.cci(14), "cci20": k.cci(20),
                "cci40": k.cci(40), "z20": k.z(20), "z50": k.z(50), "adx": dmi["adx"], "pdi": dmi["pdi"],
                "mdi": dmi["mdi"], "vsma20": k.vol_sma(20), "rsma20": k.range_sma(20), "mfi14": k.mfi(14),
                "macdLine": mac["line"], "macdSignal": mac["signal"], "macdHist": mac["hist"],
                "keltUp": kc["up"], "keltLo": kc["lo"],
            }
            for name, exp in g["series"].items():
                got = ours[name][::7].tolist()
                self.assertEqual(len(got), len(exp), name)
                for a, b in zip(got, exp):
                    if b is None:
                        self.assertFalse(math.isfinite(a), f"{sym} {name}")
                        continue
                    worst = max(worst, abs(a - b) / max(1e-12, abs(b)))
        self.assertLess(worst, 1e-11)

    def test_trades(self):
        cells = AS.grid_cells(GOLDEN["grid"])
        for sym, g in GOLDEN["symbols"].items():
            k = cache(sym)
            for pair, per_cell in g["trades"].items():
                sig = AI.pair_signal(pair, k)
                for cell, exp in zip(cells, per_cell):
                    got, _ = AS.simulate_both(pair, k.b, sig, cell)
                    rows = [[t.side, t.entry_t, t.exit_t, t.entry, t.exit, t.r, t.reason, t.bars, t.mfe, t.mae] for t in got]
                    self.assertEqual(rows, exp, f"{sym} {pair} {cell.id}")
            st = AS.stats_of(AS.simulate_both("x", k.b, AI.pair_signal(AI.PAIRS_VALIDATED[1], k), cells[0])[0])
            self.assertEqual(st["n"], g["stats"]["n"])
            self.assertAlmostEqual(st["classicPf"], g["stats"]["pf"], places=12)
            self.assertAlmostEqual(st["pf"], 1 + 0.1 * (st["net"] / st["n"]) / (AS.RT_COST * 100), places=12)
            self.assertAlmostEqual(st["wr"], g["stats"]["wr"], places=12)
            self.assertAlmostEqual(st["net"], g["stats"]["net"], places=9)


class CausalityTests(unittest.TestCase):
    def test_future_bars_never_change_the_past(self):
        candles = RAW["ETH-USDT"]
        full = cache("ETH-USDT")
        for cut in (900, 1201, 1450):
            mutated = [dict(c) for c in candles]
            for c in mutated[cut + 1:]:
                for key in ("o", "h", "l", "c"):
                    c[key] *= 1.37
                c["h"] *= 1.05
                c["v"] *= 9.0
            k = cache("ETH-USDT", mutated)
            for pair in AI.PAIRS_ALL:
                a = AI.pair_signal(pair, full)[: cut + 1]
                b = AI.pair_signal(pair, k)[: cut + 1]
                self.assertEqual(enc(a), enc(b), f"{pair} cut {cut}")

    def test_window_of_1200_bars_matches_full_history(self):
        """The live engine keeps 1,200 bars; its newest 200 states equal a full-history run."""
        for sym in ("BTC-USDT", "SOL-USDT"):
            full = cache(sym)
            win = cache(sym, RAW[sym][-1200:])
            for pair in AI.PAIRS_ALL:
                self.assertEqual(enc(AI.pair_signal(pair, full)[-200:]), enc(AI.pair_signal(pair, win)[-200:]), f"{sym} {pair}")

    def test_funding_charges_open_settlements(self):
        tr = AS.Trade("x", "S", 1, 1000.0, 5000.0, 1.0, 1.0, -0.002, "time", 2, 0, 0)
        ft = np.array([500, 1000, 3000, 5000, 6000], dtype=np.float64)
        fr = np.array([0.1, 0.2, 0.0003, 0.0002, 0.4])
        (long_,) = AS.apply_funding([tr], ft, fr)
        self.assertAlmostEqual(long_.funding, -0.0005)
        self.assertAlmostEqual(long_.r, -0.0025)
        short = AS.Trade("x", "S", -1, 1000.0, 5000.0, 1.0, 1.0, -0.002, "time", 2, 0, 0)
        self.assertAlmostEqual(AS.apply_funding([short], ft, fr)[0].funding, 0.0005)


if __name__ == "__main__":
    unittest.main()
