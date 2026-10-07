"""1h (HTF) lane engine: indicator parity with CTS-A-O, signal semantics,
CTS-A-O exit simulation and the HtfBook evidence gates."""
import json
import math
import os
import pathlib
import sys
import tempfile
import time
import unittest

import numpy as np

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-htf-"))
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server/pulse"))
import htf_engine as he  # noqa: E402

REF = json.loads((ROOT / "scripts/fixtures/htf_reference.json").read_text())
H = 3_600_000


def ref_bars() -> he.Bars:
    i = REF["input"]
    return he.Bars(np.array(i["t"], float), np.array(i["o"], float), np.array(i["h"], float),
                   np.array(i["l"], float), np.array(i["c"], float), np.array(i["v"], float), 60, "SOL-USDT")


def bars_from(closes, *, spread=0.002, vol=100.0, t0=0, opens=None, highs=None, lows=None, vols=None):
    c = np.array(closes, float)
    o = np.array(opens, float) if opens is not None else np.r_[c[0], c[:-1]]
    h = np.array(highs, float) if highs is not None else np.maximum(o, c) * (1 + spread)
    l = np.array(lows, float) if lows is not None else np.minimum(o, c) * (1 - spread)
    v = np.array(vols, float) if vols is not None else np.full(len(c), vol)
    t = t0 + np.arange(len(c)) * H
    return he.Bars(t.astype(float), o, h, l, c, v, 60, "T-USDT")


class IndicatorParity(unittest.TestCase):
    """Every indicator matches CTS-A-O's TypeScript (src/core/math/indicators.ts),
    evaluated by node on the same 600 SOL 1h bars."""

    def check(self, name, got):
        exp = REF["expected"][name]
        self.assertEqual(len(got), len(exp), name)
        for i, (g, e) in enumerate(zip(got, exp)):
            if e is None:
                self.assertFalse(np.isfinite(g), f"{name}[{i}] should be warm-up NaN, got {g}")
            else:
                self.assertTrue(math.isclose(g, e, rel_tol=1e-9, abs_tol=1e-9), f"{name}[{i}] {g} != {e}")

    def test_all_reference_vectors(self):
        b = ref_bars()
        adx, pdi, mdi = he.dmi(b.h, b.l, b.c, 14)
        _, up, lo = he.bollinger(b.c, 20, 2)
        hi20, lo20 = he.donchian_prior(b.h, b.l, 20)
        for name, got in {
            "ema21": he.ema(b.c, 21), "sma20v": he.sma(b.v, 20), "rsi7": he.rsi(b.c, 7), "rsi14": he.rsi(b.c, 14),
            "rsi21": he.rsi(b.c, 21), "atr14": he.atr(b.h, b.l, b.c, 14), "adx14": adx, "pdi14": pdi, "mdi14": mdi,
            "bbUp": up, "bbLo": lo, "cci14": he.cci(b.h, b.l, b.c, 14), "cci40": he.cci(b.h, b.l, b.c, 40),
            "z50": he.zscore(b.c, 50), "donHi20": hi20, "donLo20": lo20,
        }.items():
            with self.subTest(name):
                self.check(name, got)

    def test_flat_series_rsi_is_neutral(self):
        r = he.rsi(np.full(40, 5.0), 14)
        self.assertEqual(r[-1], 50.0)


class SignalSemantics(unittest.TestCase):
    def test_hold_keeps_the_latest_event(self):
        ev = np.array([0, 1, 0, 0, -1, 0, 0, 0, 0], np.int8)
        self.assertEqual(he.hold(ev, 3).tolist(), [0, 1, 1, 1, -1, -1, -1, 0, 0])

    def test_follow_enters_on_onsets_only_and_revert_flips(self):
        st = np.array([0, 1, 1, 1, 0, -1, -1, 1], np.int8)
        self.assertEqual(he.onset_signal(st, "follow").tolist(), [0, 1, 0, 0, 0, -1, 0, 1])
        self.assertEqual(he.onset_signal(st, "revert").tolist(), [0, -1, 0, 0, 0, 1, 0, -1])

    def test_rsi_momentum_is_mirror_symmetric(self):
        rng = np.random.default_rng(3)
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 800)))
        up = bars_from(c)
        dn = bars_from(2 * c.max() + 10 - c)  # additive mirror: every price change negated
        spec = he.KINDS["rsi-mom-14-25"]
        a = he.onset_signal(he.kind_state(spec, up), spec.bot)
        b = he.onset_signal(he.kind_state(spec, dn), spec.bot)
        self.assertGreater(int((a != 0).sum()), 5)
        self.assertEqual(a.tolist(), (-b).tolist())

    def test_every_kind_fires_both_ways_on_real_data(self):
        paths = [pathlib.Path(f"/tmp/claude-0/htf1h/{s}.json") for s in ("SOL-USDT", "BTC-USDT", "ETH-USDT", "DOGE-USDT")]
        tapes = [he.Bars.from_rows(json.loads(p.read_text())["rows"]) for p in paths if p.exists()]
        if not tapes:
            tapes = [he.Bars.from_rows([[t, [o, h, l, c, v]] for t, o, h, l, c, v in zip(*[REF["input"][k] for k in "tohlcv"])])]
        longs, shorts = set(), set()
        for b in tapes:
            for k, s in he.entry_signals(b, he.HTF_KINDS, vol_regime=False).items():
                if (s > 0).any():
                    longs.add(k)
                if (s < 0).any():
                    shorts.add(k)
        both = longs & shorts
        if len(tapes) > 1:
            self.assertEqual(sorted(set(he.HTF_KINDS) - both), [], "each kind fires long and short")
        else:
            self.assertGreater(len(both), len(he.HTF_KINDS) // 2)

    def test_4h_agreement_uses_completed_4h_bars_only(self):
        # Bars i = 0..15 (4 bars per 4h bucket). The bucket containing i is
        # published only at its last bar: map[3] = 0, map[4..6] = 0, map[7] = 1.
        b = bars_from(np.linspace(100, 110, 16), t0=0)
        _, mp = he.htf_bars(b, 4)
        self.assertEqual(mp.tolist()[:8], [-1, -1, -1, 0, 0, 0, 0, 1])

    def test_x4_state_needs_the_same_state_on_4h(self):
        spec = he.KINDS["z-50-2.5@x4"]
        b = he.Bars.from_rows([[t, [o, h, l, c, v]] for t, o, h, l, c, v in zip(*[REF["input"][k] for k in "tohlcv"])])
        base = spec.fn(b)
        hb, mp = he.htf_bars(b, 4)
        hs = spec.fn(hb)
        got = he.kind_state(spec, b)
        for i in range(b.n):
            want = base[i] if (base[i] != 0 and mp[i] >= 0 and hs[mp[i]] == base[i]) else 0
            self.assertEqual(int(got[i]), int(want), i)

    def test_vol_regime_rank_is_causal_and_bounded(self):
        rng = np.random.default_rng(9)
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 900)))
        b = bars_from(c)
        full = he.vol_regime_ok(b)
        part = he.vol_regime_ok(b.slice(0, 700))
        self.assertEqual(full[:700].tolist(), part.tolist(), "no look-ahead")
        self.assertFalse(full[: he.VOL_RANK_BARS].any())
        self.assertTrue(0.2 < full[he.VOL_RANK_BARS:].mean() < 0.8)


class ExitSimulation(unittest.TestCase):
    def test_entry_next_open_tp_and_cost(self):
        # signal on bar 0 close -> entry at bar 1 open (100); bar 2 reaches +5 %.
        b = bars_from([100, 100, 106, 106], opens=[100, 100, 101, 106], highs=[100.5, 100.5, 106, 106],
                      lows=[99.5, 99.5, 100.5, 105])
        cfg = he.ExitCfg(0.05, 0.05, 10)
        sig = np.array([1, 0, 0, 0], np.int8)
        tr = he.simulate_kind(b, "k", sig, [cfg], cost=0.002)[cfg.key]
        self.assertEqual(len(tr), 1)
        self.assertEqual((tr[0].entry_i, tr[0].exit_i, tr[0].reason), (1, 2, "tp"))
        self.assertAlmostEqual(tr[0].entry, 100.0)
        self.assertAlmostEqual(tr[0].r, 0.05 - 0.002)

    def test_stop_wins_a_bar_that_touches_both(self):
        b = bars_from([100, 100, 100], opens=[100, 100, 100], highs=[100, 100, 110], lows=[100, 100, 90])
        ex = he.exit_one(b, 1, 1, he.ExitCfg(0.05, 0.05, 10))
        self.assertEqual((ex[0], ex[2]), (2, "sl"))
        self.assertAlmostEqual(ex[1], 95.0)

    def test_gap_bar_fills_at_its_open(self):
        b = bars_from([100, 100, 90], opens=[100, 100, 90], highs=[100, 100.2, 91], lows=[100, 99.8, 89])
        ex = he.exit_one(b, 1, 1, he.ExitCfg(0.05, 0.05, 10))
        self.assertEqual(ex[0], 2)
        self.assertAlmostEqual(ex[1], 90.0, msg="gap below the stop fills at the open, not the stop")

    def test_time_exit_closes_at_the_close(self):
        b = bars_from([100, 100, 101, 102, 103], opens=[100, 100, 100, 101, 102], spread=0.001)
        ex = he.exit_one(b, 1, 1, he.ExitCfg(0.10, 0.10, 3))
        self.assertEqual(ex, (3, 102.0, "time"))

    def test_trail_arms_on_completed_bars_and_never_loosens(self):
        # entry 100; bar 1 peaks 104 (arms at 3 %); the stop moves to 104*0.97 from bar 2 on.
        b = bars_from([100, 103, 103, 100.0], opens=[100, 100, 103, 103], highs=[100, 104, 103.5, 103],
                      lows=[100, 99.5, 101.5, 100.0])
        ex = he.exit_one(b, 1, 1, he.ExitCfg(0.10, 0.05, 20, 0.03))
        self.assertEqual(ex[2], "trail")
        self.assertEqual(ex[0], 3)
        self.assertAlmostEqual(ex[1], 104 * 0.97)

    def test_short_mirrors_long(self):
        b = bars_from([100, 100, 94, 94], opens=[100, 100, 99, 94], highs=[100.5, 100.5, 99.5, 95],
                      lows=[99.5, 99.5, 94, 93.5])
        ex = he.exit_one(b, 1, -1, he.ExitCfg(0.05, 0.05, 10))
        self.assertEqual((ex[0], ex[2]), (2, "tp"))
        self.assertAlmostEqual(ex[1], 95.0)

    def test_vectorized_grid_equals_the_reference_exit(self):
        b = he.Bars.from_rows([[t, [o, h, l, c, v]] for t, o, h, l, c, v in zip(*[REF["input"][k] for k in "tohlcv"])])
        grid = he.exit_grid()
        for e in range(1, b.n - 60, 37):
            for side in (1, -1):
                fast = he.exits_for_entry(b, e, side, grid)
                for k, g in enumerate(grid):
                    self.assertEqual(fast[k], he.exit_one(b, e, side, g), (e, side, g.key))

    def test_one_slot_per_side_and_config(self):
        b = bars_from(np.full(30, 100.0), spread=0.001)
        sig = np.zeros(30, np.int8)
        sig[[2, 4, 6]] = 1
        sig[[3]] = -1
        cfg = he.ExitCfg(0.10, 0.10, 5)
        tr = he.simulate_kind(b, "k", sig, [cfg], cost=0.0)[cfg.key]
        longs = [t for t in tr if t.side > 0]
        self.assertEqual([t.signal_i for t in longs], [2], "signals inside the open position are skipped")
        self.assertEqual([t.signal_i for t in tr if t.side < 0], [3], "short slot is independent")


class PresetIntegrity(unittest.TestCase):
    def test_presets_cover_every_kind_and_parse(self):
        self.assertEqual(set(he.HTF_PRESETS), set(he.HTF_KINDS))
        for k, (key, vr) in he.HTF_PRESETS.items():
            cfg = he.parse_exit_key(key)
            self.assertEqual(cfg.key, key)
            self.assertGreater(cfg.tp, 0)
            self.assertGreater(cfg.sl, 0)
            self.assertIn(cfg.hold, (24, 48))

    def test_presets_match_the_committed_validation_report(self):
        rep = json.loads((ROOT / "reports/htf-validation-20261007/htf-cost018.json").read_text())
        for k, (key, vr) in he.HTF_PRESETS.items():
            sel = rep["kinds"][k]["plain"]
            self.assertEqual((sel["exit"], sel["volRegime"]), (key, vr), k)
        for fam in he.FAMILIES:
            self.assertTrue(rep["families"][f"{fam}|plain"]["passes"], fam)


def ev(kind, side, r, t, source="replay", sym="X-USDT"):
    return {"t": t, "r": r, "kind": kind, "side": side, "symbol": sym, "source": source}


class BookGates(unittest.TestCase):
    def book(self, **kw):
        return he.HtfBook(he.HtfSettings(enabled=True, **kw))

    def test_too_little_evidence_leaves_the_validated_preset_in_charge(self):
        b = self.book()
        b.hist["X-USDT"] = {"rsi-mom-14-25": [ev("rsi-mom-14-25", "LONG", -0.05, 1000.0 + i) for i in range(5)]}
        ok, why, info = b.gate("rsi-mom-14-25", "LONG", 1e6)
        self.assertTrue(ok, why)
        self.assertIn("preset", why)

    def test_negative_replay_evidence_closes_the_kind(self):
        b = self.book()
        b.hist["X-USDT"] = {"rsi-mom-14-25": [ev("rsi-mom-14-25", "LONG", -0.05 if i % 3 else 0.05, 1000.0 + i) for i in range(30)]}
        ok, why, _ = b.gate("rsi-mom-14-25", "LONG", 1e6)
        self.assertFalse(ok)
        self.assertIn("PF", why)

    def test_live_exchange_results_override_replay_once_they_fill_the_window(self):
        def live(b, i, pnl):
            b.on_live_close({"exchange_confirmed": True, "ind_kind": "z-50-2.5@x4", "client_id": f"c{i}", "close_fill_id": f"f{i}",
                             "t": 5e5 + i, "side": "LONG", "symbol": "X-USDT", "entry": 100.0, "qty": 1.0, "pnl": pnl})
        # Replay strongly positive, live losing: live decides once it holds the window.
        b = self.book(window=30, min_n=20)
        b.hist["X-USDT"] = {"z-50-2.5@x4": [ev("z-50-2.5@x4", "LONG", 0.06, 1000.0 + i) for i in range(60)]}
        for i in range(10):
            live(b, i, -3.0)
        ok, _, info = b.gate("z-50-2.5@x4", "LONG", 1e6)
        self.assertEqual(info["source"], "bootstrap")
        self.assertTrue(ok, "20 replay winners still fill the older slots")
        for i in range(10, 30):
            live(b, i, -3.0)
        ok, why, info = b.gate("z-50-2.5@x4", "LONG", 1e6)
        self.assertEqual(info["source"], "live")
        self.assertFalse(ok, why)
        # Replay negative, live winning: the full live window reopens the kind.
        b = self.book(window=30, min_n=20)
        b.hist["X-USDT"] = {"z-50-2.5@x4": [ev("z-50-2.5@x4", "LONG", -0.06, 1000.0 + i) for i in range(60)]}
        self.assertFalse(b.gate("z-50-2.5@x4", "LONG", 1e6)[0])
        for i in range(30):
            live(b, 100 + i, 4.0)
        ok, why, info = b.gate("z-50-2.5@x4", "LONG", 1e6)
        self.assertEqual(info["source"], "live")
        self.assertTrue(ok, why)

    def test_unconfirmed_partial_and_foreign_kind_closes_are_ignored(self):
        b = self.book()
        base = {"ind_kind": "rsi-mom-14-25", "t": 1.0, "side": "LONG", "entry": 100.0, "qty": 1.0, "pnl": 1.0}
        self.assertFalse(b.on_live_close({**base, "exchange_confirmed": False, "client_id": "a"}))
        self.assertFalse(b.on_live_close({**base, "exchange_confirmed": True, "partial": True, "client_id": "b"}))
        self.assertFalse(b.on_live_close({**base, "exchange_confirmed": True, "client_id": "c", "ind_kind": "trend"}))
        self.assertTrue(b.on_live_close({**base, "exchange_confirmed": True, "client_id": "d", "close_fill_id": "fd"}))
        self.assertFalse(b.on_live_close({**base, "exchange_confirmed": True, "client_id": "d", "close_fill_id": "fd"}), "dedup")
        self.assertAlmostEqual(b.live["rsi-mom-14-25"][0]["r"], 0.01)

    def test_direction_acceptance_blocks_a_losing_side(self):
        b = self.book(min_n=1000)
        now = 1e6
        rows = [ev("rsi-mom-14-25", "SHORT", -0.04, now - 3600 * (i % 20) - 10) for i in range(40)]
        b.hist["X-USDT"] = {"rsi-mom-14-25": rows}
        ok_s, why, _ = b.gate("rsi-mom-21-25", "SHORT", now)
        self.assertFalse(ok_s, why)
        ok_l, _, _ = b.gate("rsi-mom-21-25", "LONG", now)
        self.assertTrue(ok_l)
        ok_robust, _, _ = b.gate("z-50-2.5@x4", "SHORT", now)
        self.assertTrue(ok_robust, "acceptance is per family x side")

    def test_last_n_gate_is_optional(self):
        rows = [ev("bb-walk@x4", "LONG", 0.05, 1000.0 + i) for i in range(40)] + \
               [ev("bb-walk@x4", "LONG", -0.05, 2000.0 + i) for i in range(12)]
        b = self.book()
        b.hist["X-USDT"] = {"bb-walk@x4": rows}
        self.assertTrue(b.gate("bb-walk@x4", "LONG", 1e6)[0])
        b.s.last_n = 12
        self.assertFalse(b.gate("bb-walk@x4", "LONG", 1e6)[0])

    def test_off_or_disabled_kind_never_trades(self):
        b = he.HtfBook(he.HtfSettings(enabled=False))
        self.assertFalse(b.gate("rsi-mom-14-25", "LONG", 1.0)[0])
        b = self.book(kinds=("z-50-2.5@x4",))
        self.assertFalse(b.gate("rsi-mom-14-25", "LONG", 1.0)[0])

    def test_bars_merge_only_closed_bars_and_signals_once_per_bar(self):
        b = self.book()
        rows = [[t, *vals] for t, vals in zip(REF["input"]["t"], zip(*[REF["input"][k] for k in "ohlcv"]))]
        now_ms = rows[-1][0] + H - 1  # the last bar is still forming
        n = b.set_bars("SOL-USDT", rows, now_ms=now_ms)
        self.assertEqual(n, len(rows) - 1)
        b.new_signals("SOL-USDT")
        self.assertEqual(b.new_signals("SOL-USDT"), [], "a bar is decided once")
        b.set_bars("SOL-USDT", rows, now_ms=now_ms + 2)
        self.assertEqual(b.bars["SOL-USDT"].n, len(rows))

    def test_replay_rows_are_closed_trades_with_preset_exits(self):
        b = self.book()
        rows = [[t, *vals] for t, vals in zip(REF["input"]["t"], zip(*[REF["input"][k] for k in "ohlcv"]))]
        b.set_bars("SOL-USDT", rows)
        n = b.replay("SOL-USDT")
        self.assertGreater(n, 0)
        for kind, rs in b.hist["SOL-USDT"].items():
            cfg, _ = he.preset(kind)
            for r in rs:
                self.assertGreaterEqual(r["r"], -cfg.sl - 0.05 - b.s.cost, kind)  # gap fills can exceed the stop a little
                self.assertLessEqual(r["t"], rows[-1][0] / 1000 + 3600)

    def test_settings_from_overlay(self):
        s = he.HtfSettings.from_overlay({"htfEnabled": True, "htfKinds": ["z-50-2.5@x4", "nope"], "htfMinPf": 1.2,
                                          "htfLastN": 12, "positionCostPct": 0.18, "htfMaxOpen": 4})
        self.assertTrue(s.enabled)
        self.assertEqual(s.kinds, ("z-50-2.5@x4",))
        self.assertEqual((s.min_pf, s.last_n, s.max_open), (1.2, 12, 4))
        self.assertAlmostEqual(s.cost, 0.0018)
        self.assertFalse(he.HtfSettings.from_overlay({}).enabled)


if __name__ == "__main__":
    unittest.main()
