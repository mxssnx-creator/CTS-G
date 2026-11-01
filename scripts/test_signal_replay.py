"""Offline Signal model contracts, with independent cash/clock oracles."""
import copy
import json
import math
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "pulse"))
import signal_replay as model
from signal_sources import SOURCES, completed_bars, onsets, states, validate_bars, warmup_bars
from signal_scalar_oracle import normal_stages

T0 = 1791460800000


def config():
    return json.loads((ROOT / "presets" / "signals-offline-research.json").read_text())


def fixture_cfg():
    cfg = config()
    cfg.update(sources=["ema-cross"], ranges=["short"], timeframes=[1], lastNLanes=[{"base": 1, "main": 1, "real": 1}])
    cfg["protections"] = cfg["protections"][:1]
    return cfg


def fixture_bars(n=180):
    return [[T0 + i * model.MINUTE, 100, 102, 99.9, 101, 1] for i in range(n)]


class SignalContracts(unittest.TestCase):
    def test_strict_schema_and_effective_switches(self):
        model.validate_config(config())
        for key, value in (("sources", ["not-a-signal"]), ("timeframes", [2]), ("mode", "live"), ("minPf", 2.199), ("normalEnabled", "false")):
            raw = config(); raw[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): model.validate_config(raw)
        raw = config(); raw["unusedMagicProfit"] = True
        with self.assertRaises(ValueError): model.validate_config(raw)

    def test_no_dust_or_unsupported_protection(self):
        for change in ({"trailGap": .0001}, {"kind": "dca-active"}, {"sl": 0}):
            raw = config(); raw["protections"][1].update(change)
            with self.assertRaises(ValueError): model.validate_config(raw)

    def test_data_gaps_and_duplicate_minutes_fail(self):
        rows = fixture_bars(5)
        for bad in (rows[:2] + rows[3:], rows + rows[-1:], [[*rows[0][:4], math.nan, 1]]):
            with self.assertRaises(ValueError): validate_bars(bad)

    def test_timeframes_are_closed_and_utc_aligned(self):
        rows = fixture_bars(11)
        self.assertEqual(len(completed_bars(rows, 5)), 2)
        self.assertEqual(len(completed_bars(rows[1:], 5)), 1)
        self.assertEqual(completed_bars(rows[:4], 5), [])

    def test_sources_and_onsets_are_prefix_causal(self):
        rng = random.Random(884)
        closes = [100 + 4 * math.sin(i / 9) + rng.random() for i in range(500)]
        highs, lows = [x+1 for x in closes], [x-1 for x in closes]
        for source in SOURCES:
            for range_name in ("short", "medium"):
                volumes = [1+i%7 for i in range(len(closes))]
                full = states(closes, source, range_name, highs, lows, volumes)
                for count in (20, 50, 123, 270): self.assertEqual(states(closes[:count], source, range_name, highs[:count], lows[:count], volumes[:count]), full[:count])
                bars = [[T0+i*60000, x, x+1, x-1, x, 1] for i,x in enumerate(closes)]
                prefix = onsets(bars[:300], source, range_name, 5)
                self.assertEqual(prefix, {t:s for t,s in onsets(bars, source, range_name, 5).items() if t <= T0+300*60000})

    def test_source_warmup_does_not_invent_missing_prehistory(self):
        self.assertEqual(warmup_bars("ema-cross", "medium"), 55)
        cfg = fixture_cfg(); cfg["timeframes"] = [30]; cfg["ranges"] = ["medium"]
        result = model.replay({"A":fixture_bars()}, cfg, T0, T0+model.HOUR, T0+3*model.HOUR)
        coverage = result["sourceCoverage"][0]
        self.assertEqual(coverage["prehistoryBars"], 2)
        self.assertFalse(coverage["sourceWarmAtEnd"])
        self.assertEqual(result["modelCounts"]["ordersOpened"], 0)

    def test_all_cost_pairs_and_sides_match_cash_oracle(self):
        cfg = config(); p = cfg["protections"][0]
        for side in (-1, 1):
            for a in ("maker", "taker"):
                for b in ("maker", "taker"):
                    fees = {**cfg["fees"], "entryLiquidity": a, "exitLiquidity": b}
                    pos = model.open_position(T0, 100, side, p, fees, 125)
                    got = model.close_result(pos, 101, T0+60000, "oracle", fees)
                    entry, exit_ = 100*(1+side*.0001), 101*(1-side*.0001)
                    qty = 125/entry
                    expected = qty*(exit_-entry)*side - 125*fees[a] - qty*exit_*fees[b]
                    self.assertAlmostEqual(got["netCash"], expected, places=12)
                    self.assertAlmostEqual(got["unitNet"], expected/125, places=12)

    def test_same_bar_ambiguity_is_stop_first_and_gap_is_not_filled_at_stop(self):
        cfg = config(); p, fees = cfg["protections"][0], cfg["fees"]
        for side in (-1, 1):
            pos = model.open_position(T0, 100, side, p, fees)
            got = model.advance_position(pos, [T0, 100, 110, 90, 100, 1], p, fees)
            self.assertEqual(got["reason"], "stop"); self.assertLess(got["unitNet"], 0)
            pos = model.open_position(T0, 100, side, p, fees)
            price = 95 if side > 0 else 105
            got = model.advance_position(pos, [T0+60000, price, price+1, price-1, price, 1], p, fees)
            self.assertEqual(got["reason"], "gap-stop")
            self.assertAlmostEqual(got["exit"], price*(1-side*fees["slippagePerLeg"]))

    def test_trailing_never_uses_unknown_extreme_order(self):
        cfg = config(); p, fees = cfg["protections"][2], cfg["fees"]
        pos = model.open_position(T0, 100, 1, p, fees)
        self.assertIsNone(model.advance_position(pos, [T0, 100, 102, 99.9, 101.9, 1], p, fees))
        self.assertGreater(pos.stop, 101)
        result = model.advance_position(pos, [T0+60000, 101.9, 102, 101, 101.2, 1], p, fees)
        self.assertEqual(result["reason"], "stop")

    def test_five_percent_trailing_only_has_no_fixed_target(self):
        cfg = config()
        p = {"id":"wide", "kind":"trailing-only", "tp":0, "sl":.01,
             "trailArm":.055, "trailGap":.05, "holdMinutes":120}
        cfg["protections"] = [p]
        model.validate_config(cfg)
        fees = {**cfg["fees"], "slippagePerLeg":0}
        for side in (-1,1):
            pos = model.open_position(T0,100,side,p,fees)
            first = [T0,100,106,99.9,105.9,1] if side==1 else [T0,100,100.1,94,94.1,1]
            self.assertIsNone(model.advance_position(pos,first,p,fees), "crossing +1% is NOT a fixed TP")
            self.assertAlmostEqual(pos.stop,101 if side==1 else 99)
            next_bar = [T0+60000,105.9,107,100.5,101,1] if side==1 else [T0+60000,94.1,99.5,93,99,1]
            closed = model.advance_position(pos,next_bar,p,fees)
            self.assertEqual(closed["reason"],"stop")
            self.assertGreater(closed["netCash"],0)
        cfg["protections"][0]["tp"] = .01
        with self.assertRaises(ValueError):model.validate_config(cfg)

    def test_prehistory_selection_is_frozen_and_does_not_read_future(self):
        cfg = fixture_cfg()
        bars = fixture_bars()
        events = {T0+i*60000:1 for i in range(0,180,3)}
        with patch.object(model,"onsets",return_value=events):
            one = model.replay({"A":bars},cfg,T0,T0+model.HOUR,T0+3*model.HOUR,freeze_prehistory=True)
            changed = copy.deepcopy(bars)
            for r in changed[60:]:r[1:5] = [100,101,90,91]
            two = model.replay({"A":changed},cfg,T0,T0+model.HOUR,T0+3*model.HOUR,freeze_prehistory=True)
        self.assertEqual(one["prehistorySelection"],two["prehistorySelection"])
        selected = set(one["prehistorySelection"]["chainQualifiedIds"])
        self.assertTrue(selected)
        self.assertTrue(all(r["context"] in selected for r in one["trades"]+one["openPositions"]))
        cfg["lastNLanes"] = [{"base":200,"main":200,"real":200}]
        with patch.object(model,"onsets",return_value=events):
            empty = model.replay({"A":bars},cfg,T0,T0+model.HOUR,T0+3*model.HOUR,freeze_prehistory=True)
        self.assertEqual(empty["prehistorySelection"]["chainQualifiedIds"],[])
        self.assertEqual(empty["modelCounts"]["ordersOpened"],0)

    def test_stage_exact_clock_and_empty_no_loss_states(self):
        stage = model.Stage()
        stage.rows.extend([{"exitT": T0, "unitNet": -.01}, {"exitT": T0+60000, "unitNet": .1}])
        self.assertEqual(stage.figures(T0+60000, 1, 60)["net"], -.01)
        self.assertEqual(stage.figures(T0+60001, 1, 60)["pfState"], "no-loss")
        self.assertFalse(model.qualifies(model.evidence([]), 1, config()))
        self.assertEqual(model.evidence([1, -.001])["pf"], 1000)

    def run_fixture(self, cfg=None, data=None):
        data = data or {"A": fixture_bars()}
        with patch.object(model, "onsets", return_value={T0+i*60000: 1 for i in range(0,180,3)}):
            return model.replay(data, cfg or fixture_cfg(), T0, T0+model.HOUR, T0+3*model.HOUR)

    def test_processed_stages_fill_and_admit_separately(self):
        result = self.run_fixture()
        ctx = next(c for c in result["contexts"] if c["side"] == 1)
        self.assertGreater(ctx["stages"]["base"]["closes"], ctx["stages"]["main"]["closes"])
        self.assertGreater(ctx["stages"]["main"]["closes"], ctx["stages"]["real"]["closes"])
        self.assertGreater(result["modelCounts"]["ordersSettled"], 0)
        self.assertAlmostEqual(result["finalEquity"]-result["initialEquity"], sum(r["netCash"] for r in result["trades"]), places=9)

    def test_stage_histories_match_independent_normal_oracle(self):
        rng = random.Random(831)
        bars, price = [], 100
        for i in range(240):
            close = price * (1 + rng.choice([-.012, -.003, .004, .015, .02]))
            bars.append([T0+i*60000, price, max(price,close)*1.002, min(price,close)*.998, close, 1])
            price = close
        events = {r[0]: 1 if i % 5 else -1 for i,r in enumerate(bars) if i % 2 == 0}
        cfg = fixture_cfg(); cfg["lastNLanes"] = [{"base":2,"main":2,"real":1}, {"base":4,"main":3,"real":2}]
        cfg["historyMinutes"] = 60
        for entry in ("maker", "taker"):
            cfg["fees"]["entryLiquidity"] = entry
            with patch.object(model,"onsets",return_value=events):
                result = model.replay({"A":bars},cfg,T0,T0+model.HOUR,T0+4*model.HOUR)
            for ctx in result["contexts"]:
                closed, opened = normal_stages(bars,events,ctx["protection"],ctx["lastN"],cfg["fees"],60,cfg["minPf"],.04,ctx["side"])
                for stage, evidence in ctx["stages"].items():
                    with self.subTest(entry=entry,lane=ctx["lastN"],side=ctx["side"],stage=stage):
                        values = [v for t,v in closed[stage] if T0+3*model.HOUR <= t < T0+4*model.HOUR][-ctx["lastN"][stage]:]
                        self.assertEqual(evidence["closes"],len(closed[stage]))
                        self.assertEqual(evidence["open"],opened[stage] is not None)
                        self.assertEqual(evidence["n"],len(values))
                        self.assertAlmostEqual(evidence["net"],sum(values),places=12)
                        self.assertAlmostEqual(evidence["cumulativeReturns"]["net"],sum(v for _,v in closed[stage]),places=12)

    def test_future_data_cannot_rewrite_prior_marks_or_progress(self):
        bars = fixture_bars(240)
        events = {r[0]:1 for i,r in enumerate(bars) if i%3 == 0}
        with patch.object(model,"onsets",return_value=events):
            first = model.replay({"A":bars[:180]},fixture_cfg(),T0,T0+model.HOUR,T0+3*model.HOUR)
            for i in range(180,240): bars[i] = [T0+i*60000,100,100,90,91,1]
            later = model.replay({"A":bars},fixture_cfg(),T0,T0+model.HOUR,T0+4*model.HOUR)
        self.assertEqual(first["marks"],later["marks"][:len(first["marks"])])
        self.assertEqual(first["stageProgress"],later["stageProgress"][:3])

    def test_same_clock_risk_limits_and_counts_reconcile(self):
        cfg = fixture_cfg(); cfg["risk"].update(maxOpen=1,maxGrossExposure=25)
        result = self.run_fixture(cfg,{"A":fixture_bars(),"B":fixture_bars()})
        self.assertLessEqual(max(r["openOrders"] for r in result["marks"]),1)
        count = result["modelCounts"]
        self.assertEqual(count["ordersOpened"],count["ordersSettled"]+count["ordersOpen"])
        self.assertEqual(count["ordersOpen"],len(result["openPositions"]))
        self.assertGreater(result["riskRejections"].get("max-open",0),0)

    def test_other_symbols_sides_and_ranges_cannot_change_own_history(self):
        a = self.run_fixture()
        b = self.run_fixture(data={"A": fixture_bars(), "B": fixture_bars()})
        self.assertEqual(a["contexts"], [c for c in b["contexts"] if c["symbol"] == "A"])
        for c in a["contexts"]:
            if c["side"] == -1: self.assertTrue(all(s["closes"] == 0 for s in c["stages"].values()))
        cfg = fixture_cfg(); cfg["lastNLanes"].append({"base": 12, "main": 8, "real": 4})
        c = self.run_fixture(cfg)
        self.assertEqual(a["contexts"], [x for x in c["contexts"] if x["lastN"]["base"] == 1])

    def test_normal_off_preserves_history_and_trailing_off_removes_trails(self):
        cfg = fixture_cfg(); cfg["normalEnabled"] = False
        result = self.run_fixture(cfg)
        self.assertEqual(result["modelCounts"]["ordersOpened"], 0)
        self.assertGreater(sum(c["stages"]["base"]["closes"] for c in result["contexts"]), 0)
        cfg["protections"] = config()["protections"]; cfg["trailingEnabled"] = False
        self.assertTrue(all(c["protection"]["kind"] == "normal" for c in self.run_fixture(cfg)["contexts"]))

    def test_drawdown_and_hourly_loss_pause_new_entries_not_managed_exits(self):
        cfg = fixture_cfg(); cfg["risk"].update(notionalPerOrder=100, maxDrawdown=.0005, maxHourlyLoss=.0005)
        bars = fixture_bars()
        for i in range(60,180): bars[i] = [T0+i*60000, 100, 100.1, 98, 99, 1]
        result = self.run_fixture(cfg, {"A": bars})
        self.assertTrue(result["drawdownLatched"])
        self.assertGreater(result["modelCounts"]["ordersSettled"], 0)
        self.assertFalse(result["hourly"]["everyHourNetPositive"])
        self.assertEqual(result["modelCounts"]["ordersOpen"], 0)

    def test_open_gap_drawdown_is_reported_even_when_close_recovers(self):
        cfg=fixture_cfg();cfg["risk"]["maxDrawdown"] = .0001
        bars=fixture_bars()
        bars[60]=[T0+60*60000,100,100.2,99.9,100.1,1]
        bars[61]=[T0+61*60000,99.5,100.2,99.4,100.1,1]
        result=self.run_fixture(cfg,{"A":bars})
        self.assertTrue(result["drawdownLatched"])
        self.assertGreaterEqual(result["maxEquityDrawdown"],.0001)
        mark=next(x for x in result["admissionMarks"] if x["t"]==T0+61*60000)
        self.assertLess(mark["equity"],999.9)
        self.assertEqual([r["entrySequence"] for r in sorted(result["trades"],key=lambda r:r["entryT"])],list(range(1,len(result["trades"])+1)))

    def test_hourly_net_checks_mtm_and_counts_empty_unknown_hours(self):
        marks = [{"t":T0,"equity":100},{"t":T0+model.HOUR,"equity":99},{"t":T0+2*model.HOUR,"equity":99}]
        trade = {"exitT":T0+model.HOUR,"unitNet":.1,"netCash":5}
        audit = model.hourly_audit(marks,[trade],T0,T0+3*model.HOUR)
        self.assertEqual([r["state"] for r in audit["hours"]], ["negative","flat","unknown"])
        self.assertEqual(audit["hours"][0]["settledOrders"],1)
        self.assertFalse(audit["everyHourNetPositive"])
        with self.assertRaises(ValueError): model.hourly_audit(marks+marks[:1],[],T0,T0+model.HOUR)


if __name__ == "__main__": unittest.main()
