"""Desk live edge guard: a direction whose own last-N confirmed exchange round
trips are net negative trades probe lots only, until the edge returns."""
import os
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace as NS

os.environ.setdefault("CTS_DATA_DIR", tempfile.mkdtemp(prefix="cts-live-edge-"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
import pulse_trader as pt  # noqa: E402

COST = 0.1
T0 = time.time() - 100000


def gross(pf):
    return (1.0 + (pf - 1.0) / 0.1) * COST / 100.0


class Seq:
    i = 0


def close(pf, side="LONG", conn=None, **extra):
    Seq.i += 1
    conn = conn or pt.CONN_SHORT
    base = dict(t=T0 + Seq.i * 60, symbol="X-USDT", side=side, qty=1.0, entry=100.0, exit=100.0,
                pnl=1.0 if pf >= 1 else -1.0, pnl_pct=gross(pf), reason="tp", hold_s=60.0,
                set_id="S", client_id=f"e{Seq.i}", exchange_confirmed=True, conn=conn,
                system_id=pt.SYSTEM_ID, tracking_scope=pt.tracking_scope(conn, pt.SYSTEM_ID))
    base.update(extra)
    return pt.Closed(**base)


def pulse(n=50):
    p = object.__new__(pt.Pulse)
    p.closed = []
    p.position_cost_pct = COST
    p.max_book_notional = lambda *a, **k: 1e9
    p.sets = NS(real_min_pf=1.10)
    p.live_edge_guard = True
    p.live_edge_n = n
    p.live_edge_probe_share = 0.25
    return p


class LiveEdgeGuardTests(unittest.TestCase):
    def test_learning_until_the_window_is_full(self):
        p = pulse()
        p.closed = [close(0.5) for _ in range(49)]
        self.assertEqual(p.live_edge_state("LONG")["state"], "learning")

    def test_net_negative_window_probes_and_recovers_only_at_the_floor(self):
        p = pulse()
        p.closed = [close(0.5) for _ in range(50)]
        st = p.live_edge_state("LONG")
        self.assertEqual(st["state"], "probe")
        self.assertLess(st["pf"], 1.0)
        # Back to break-even: still guarded (hysteresis up to the Real floor).
        p.closed = p.closed + [close(1.5) for _ in range(25)]
        self.assertGreaterEqual(p.live_edge_state("LONG")["pf"], 1.0)
        self.assertLess(p.live_edge_state("LONG")["pf"], 1.10)
        self.assertEqual(p.live_edge_state("LONG")["state"], "probe")
        p.closed = p.closed + [close(1.4) for _ in range(50)]
        self.assertEqual(p.live_edge_state("LONG")["state"], "edge")

    def test_directions_are_independent(self):
        p = pulse()
        p.closed = [close(0.5, "LONG") for _ in range(50)] + [close(1.4, "SHORT") for _ in range(50)]
        self.assertEqual(p.live_edge_state("LONG")["state"], "probe")
        self.assertEqual(p.live_edge_state("SHORT")["state"], "edge")

    def test_other_desk_unconfirmed_and_partial_closes_never_count(self):
        p = pulse()
        other = "bingx-x01" if pt.CONN_SHORT != "bingx-x01" else "bingx-x02"
        p.closed = ([close(0.5, conn=other) for _ in range(50)]
                    + [close(0.5, exchange_confirmed=False) for _ in range(50)]
                    + [close(0.5, partial=True) for _ in range(50)])
        self.assertEqual(p.live_edge_state("LONG")["state"], "learning")

    def test_guard_off_setting(self):
        p = pulse()
        p.live_edge_guard = False
        p.closed = [close(0.5) for _ in range(50)]
        self.assertEqual(p.live_edge_state("LONG")["state"], "off")

    def test_snapshot_publishes_both_directions(self):
        p = pulse()
        p.closed = [close(0.5) for _ in range(50)]
        snap = p._live_edge_snapshot()
        self.assertEqual(snap["LONG"]["state"], "probe")
        self.assertEqual(snap["SHORT"]["state"], "learning")
        self.assertEqual(snap["n"], 50)

    def test_profile_and_overlays_ship_the_guard_on(self):
        import json
        import connection_profile as cp
        prof = cp.processing_profile()
        self.assertIs(prof["liveEdgeGuard"], True)
        self.assertEqual(prof["liveEdgeN"], 50)
        root = pathlib.Path(__file__).resolve().parents[1] / "server/pulse"
        for lane in ("bingx-x01", "bingx-x02"):
            ov = json.loads((root / f"overlay-{lane}.json").read_text())
            self.assertIs(ov["liveEdgeGuard"], True)
            self.assertEqual(ov["liveEdgeN"], 50)
            self.assertAlmostEqual(ov["liveEdgeProbeShare"], 0.25)


class OneMinuteLanesSwitchTests(unittest.TestCase):
    def test_probe_forces_minimum_lots_on_both_sides_even_with_a_good_tape(self):
        p = pulse()
        p.closed = [close(2.0) for _ in range(50)]
        self.assertEqual(p.live_edge_state("LONG")["state"], "edge")
        p.one_minute_lanes = "probe"
        for side in ("LONG", "SHORT"):
            st = p.live_edge_state(side)
            self.assertEqual((st["state"], st.get("forced")), ("probe", True))
        self.assertEqual(p._live_edge_snapshot()["oneMinuteLanes"], "probe")

    def test_off_blocks_new_1m_entries_but_not_the_1h_lane(self):
        p = object.__new__(pt.Pulse)
        p.one_minute_lanes = "off"
        p._entry_mode_flags = lambda *a: (True, True, True)
        p.entries_blocked = lambda: False
        steps = p._place_steps("X-USDT", 1, "ind:x", 0.9, None, None, "normal")
        self.assertEqual(list(steps), [])
        # the 1h row passes the switch and reaches the next check (sets readiness)
        p.sets = NS(enabled=True, use_historic_gate=True, progress=NS(ready=False))
        with self.assertRaises(AttributeError):
            list(p._place_steps("X-USDT", 1, "htf:k", 0.9, {"lane": "htf", "id": "htf:k:x"}, None, "normal"))

    def test_live_ships_on_probe_vst_on(self):
        import json
        root = pathlib.Path(__file__).resolve().parents[1] / "server/pulse"
        self.assertEqual(json.loads((root / "overlay-bingx-x01.json").read_text())["oneMinuteLanes"], "probe")
        self.assertEqual(json.loads((root / "overlay-bingx-x02.json").read_text())["oneMinuteLanes"], "on")


if __name__ == "__main__":
    unittest.main()
