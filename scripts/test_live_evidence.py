"""Strategies, adjustments and coordination are decided by live exchange results.

Every gate takes its stage window N: live exchange round trips alone once they
hold N; below that, replay only fills the missing (older) slots and live rows
stay the newest. Unconfirmed, partial and foreign closes never count, and each
desk reads only its own connection."""
import os
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-live-evidence-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_trader as pt  # noqa: E402
from position_cost import live_first_window  # noqa: E402
from set_engine import SetBook, SetState  # noqa: E402

COST = 0.1
T0 = time.time() - 400000


def gross(pf):
    return (1.0 + (pf - 1.0) / 0.1) * COST / 100.0


def row(i, pf, side="LONG", **extra):
    out = {"t": T0 + i * 300, "symbol": "X-USDT", "side": side, "pnl": 1.0 if pf >= 1 else -1.0,
           "pnl_pct": gross(pf), "hold_s": 60, "reason": "tp" if pf >= 1 else "sl",
           "qty": 1.0, "entry": 100.0, "strategy": "core"}
    out.update(extra)
    return out


def live(i, pf, st, **extra):
    return row(i, pf, set_id=st.id, client_id=f"c{i}", close_fill_id=f"f{i}",
               exchange_confirmed=True, **extra)


def book(**extra):
    ov = dict(setStrictGate=False, setUseHistoricGate=False, entryPolicy="permissive-bounded",
              minPf=1.15, baseEvalPosCount=30, setPfWindow=30, setDeactN=25,
              mainEvalPosCount=30, realEvalPosCount=30,
              positionCostPct=COST, setMaxDdTimeS=10 ** 7, setLiveNegativeDeact=False)
    ov.update(extra)
    b = SetBook()
    b.load(ov, rebuild=False)
    b.progress.ready = True
    st = SetState(id="general:1m:sl0.6:st3", pack="general", tf="1m", sl_ratio=0.6,
                  trail_key="", trail_arm=0, trail_give=0, step=3, tp_pct=0.0045, idx=0)
    b.sets = {st.id: st}
    b.by_idx = [st]
    b._reindex()
    return b, st


class WindowRule(unittest.TestCase):
    def test_replay_fills_only_missing_slots_and_live_stays_newest(self):
        replay = [{"t": float(i)} for i in range(100, 200)]
        lv = [{"t": float(i), "live": True} for i in range(5)]  # older stamps than replay
        window, src = live_first_window(lv, replay, 30)
        self.assertEqual(src, "bootstrap")
        self.assertEqual(len(window), 30)
        self.assertTrue(all(r.get("live") for r in window[-5:]), "live rows are the newest slots")
        self.assertEqual([r["t"] for r in window[:25]], [float(i) for i in range(175, 200)])

    def test_full_live_window_ignores_replay(self):
        lv = [{"t": float(i)} for i in range(40)]
        window, src = live_first_window(lv, [{"t": 1e9}] * 50, 30)
        self.assertEqual(src, "live")
        self.assertEqual([r["t"] for r in window], [float(i) for i in range(10, 40)])


class SetStageGate(unittest.TestCase):
    def test_live_losers_close_base_although_replay_is_strongly_positive(self):
        b, st = book()
        st.hist = [row(i, 1.60) for i in range(60)]
        b._score_one(st)
        self.assertTrue(st.stage_ledger.get("base"), "replay bootstraps Base")
        for i in range(29):
            b.on_live_close(live(1000 + i, 0.5, st))
        side = st.by_side["LONG"]
        self.assertEqual(side["evidenceSource"], "bootstrap")
        self.assertEqual(side["liveN"], 29)
        n = b._evidence_window()
        for i in range(29, n):
            b.on_live_close(live(1000 + i, 0.5, st))
        side = st.by_side["LONG"]
        self.assertEqual(side["evidenceSource"], "live")
        self.assertFalse(st.stage_ledger.get("base"), "live alone decides once it holds N")
        self.assertFalse(b._base_metrics_ok(side))

    def test_live_winners_keep_the_set_after_replay_turns_negative(self):
        b, st = book()
        st.hist = [row(i, 0.5) for i in range(60)]
        for i in range(b._evidence_window()):
            b.on_live_close(live(1000 + i, 1.60, st))
        self.assertEqual(st.by_side["LONG"]["evidenceSource"], "live")
        self.assertTrue(st.stage_ledger.get("base"))

    def test_unconfirmed_and_partial_closes_never_count(self):
        b, st = book()
        b.on_live_close(dict(live(1, 0.5, st), exchange_confirmed=False))
        b.on_live_close(dict(live(2, 0.5, st), partial=True))
        st.live.append(row(3, 0.5, set_id=st.id))  # no confirmed flag at all
        self.assertEqual(st.evaluation_live(), [])


class IndicationGates(unittest.TestCase):
    def test_kind_and_range_close_on_live_losers(self):
        b, _ = book()
        b.ind_hist["trend"] = [row(i, 1.60, ind_config="trend:21") for i in range(60)]
        self.assertTrue(b.ind_config_stats("trend", "trend:21", "LONG")["validated"])
        for i in range(b.eval_need()):
            b.on_live_close(dict(row(1000 + i, 0.5, ind_config="trend:21", ind_kind="trend"),
                                 client_id=f"k{i}", close_fill_id=f"kf{i}", exchange_confirmed=True))
        cfg = b.ind_config_stats("trend", "trend:21", "LONG")
        self.assertEqual(cfg["source"], "live")
        self.assertFalse(cfg["validated"])
        kind = b.ind_stats("trend", "LONG")
        self.assertEqual(kind["source"], "live-exchange")
        self.assertFalse(kind["validated"])

    def test_only_confirmed_core_closes_reach_the_kind_tape(self):
        b, _ = book()
        b.on_live_close(dict(row(1, 1.2, ind_kind="trend"), client_id="a", exchange_confirmed=False))
        b.on_live_close(dict(row(2, 1.2, ind_kind="trend"), client_id="b", exchange_confirmed=True, partial=True))
        b.on_live_close(dict(row(3, 1.2, ind_kind="trend", strategy="core+dca"), client_id="c", exchange_confirmed=True))
        self.assertEqual(b.ind_live.get("trend") or [], [])
        b.on_live_close(dict(row(4, 1.2, ind_kind="trend"), client_id="d", exchange_confirmed=True))
        self.assertEqual(len(b.ind_live["trend"]), 1)


class BlockMainGate(unittest.TestCase):
    def test_live_block_round_trips_override_replay(self):
        b, st = book()
        b.strategy_hist = {"block": [row(i, 1.60, block_count=1, strategy="block", set_id=st.id, ind_kind="")
                                     for i in range(80)]}
        b.score_block_main()
        self.assertTrue(b.block_main_live_ok(1))
        need = max(5, min(75, int(b.block_eval_pos or 50)))
        for i in range(need):
            b.on_live_close(dict(live(2000 + i, 0.5, st), strategy="core+block", block_count=1))
        blob = b.block_main_eval[(1, "", "")]
        self.assertEqual(blob["evidenceSource"], "live")
        self.assertFalse(b.block_main_live_ok(1))


def pulse(closed):
    p = object.__new__(pt.Pulse)
    p.closed = closed
    p.position_cost_pct = COST
    p.max_book_notional = lambda *a, **k: 1e9
    return p


def closed(i, pnl, *, strategy="core", conn=None, **extra):
    base = dict(t=T0 + i * 60, symbol="X-USDT", side="LONG", qty=1.0, entry=100.0, exit=100.0,
                pnl=pnl, pnl_pct=pnl / 100.0, reason="tp", hold_s=60.0, set_id="S", client_id=f"p{i}",
                exchange_confirmed=True, strategy=strategy, conn=conn or pt.CONN_SHORT,
                system_id=pt.SYSTEM_ID, tracking_scope=pt.tracking_scope(conn or pt.CONN_SHORT, pt.SYSTEM_ID))
    base.update(extra)
    return pt.Closed(**base)


class TraderEvidence(unittest.TestCase):
    def test_each_desk_reads_only_its_own_confirmed_round_trips(self):
        other = "bingx-x01" if pt.CONN_SHORT != "bingx-x01" else "bingx-x02"
        p = pulse([closed(1, 1.0), closed(2, -1.0, conn=other), closed(3, -1.0, exchange_confirmed=False),
                   closed(4, -1.0, partial=True)])
        ids = [r.client_id for r in p.live_closes()]
        self.assertEqual(ids, ["p1"])

    def test_combined_strategy_closes_count_for_dca_and_block(self):
        p = pulse([closed(1, 1.0, strategy="core+dca"), closed(2, 1.0, strategy="core+block"),
                   closed(3, 1.0, strategy="core")])
        self.assertEqual([r.client_id for r in p.config_strategy_closes("S", "LONG", strategy="dca")], ["p1"])
        self.assertEqual([r.client_id for r in p.config_strategy_closes("S", "LONG", strategy="block")], ["p2"])

    def test_old_live_losses_keep_counting_after_three_hours(self):
        old = time.time() - 6 * 3600
        p = pulse([])
        rows = [NS(t=old + i, side="LONG", pnl=-1.0, pnl_pct=-0.01, qty=1.0, entry=100.0) for i in range(8)]
        pf = p.live_recent_pf("LONG", n=8, rows=rows)
        self.assertIsNotNone(pf)
        self.assertLess(pf, 1.0)

    def test_proven_negative_live_real_stops_test_historic_block_adds(self):
        b, st = book()
        p = pulse([])
        p.sets = b
        b.hist_test_set_ids = {st.id}
        p._hist_test_owns_catalog = lambda: True
        st.by_side = {"LONG": {"evidenceSource": "bootstrap", "last15_n": 20, "last15_ratio": 1.3,
                               "real_pf": 0.0, "real_n": 0}}
        pos = NS(set_id=st.id, side="LONG")
        self.assertGreater(p.block_intern_pf(pos), 0.0, "intern fallback while live is short")
        st.by_side["LONG"]["evidenceSource"] = "live"
        self.assertEqual(p.block_intern_pf(pos), 0.0)

    def test_unconfirmed_close_runs_block_lifecycle_without_a_ring_sample(self):
        from block_engine import BlockBook
        bb = BlockBook(os.path.join(os.environ["CTS_DATA_DIR"], "le-block.json"), {})
        lane = bb.register_parent("X-USDT", "LONG", 1.0, 100.0)
        bb.on_parent_close("X-USDT", "LONG", -1.0, pnl_pct=-0.01, record=False)
        self.assertEqual(lane.parent_pf_ring, [])
        self.assertFalse(lane.active)


if __name__ == "__main__":
    unittest.main()
