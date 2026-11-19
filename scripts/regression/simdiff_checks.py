"""Simulation against live: the gap between the replay's result and the exchange's result of the same close is measured once and adds up."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from live_sim_diff import CONN, HOST, main
from sim_diff import diff_note, diff_report, new_diff, bucket_of


def row(reason, gross, net, sl=0.01, tp=0.02, pack="p", extra=None):
    r = {"reason": reason, "pnl_pct": gross, "net_pnl_pct": net, "sl_pct": sl, "tp_pct": tp, "pack": pack}
    r.update(extra or {})
    return r


def gap_adds_up():
    a = new_diff()
    cost = 0.0012
    diff_note(a, row("sl", -0.015, -0.0165), cost)        # stop filled 1.5x the planned 1.0 %, live cost 0.15 %
    diff_note(a, row("tp", 0.02, 0.0188), cost)           # target filled at the level, live cost 0.12 %
    diff_note(a, row("trail", 0.004, 0.0028), cost)       # no planned level: only the cost gap exists
    r = diff_report(a)
    t = r["total"]
    assert r["closes"] == 3 and r["unknown"] == 0
    # plain reading: sim nets -0.0112, +0.0188, +0.0028; live nets -0.0165, +0.0188, +0.0028
    assert abs(t["simNetPct"] - 100 * (-0.0112 + 0.0188 + 0.0028)) < 1e-9, t
    assert abs(t["liveNetPct"] - 100 * (-0.0165 + 0.0188 + 0.0028)) < 1e-9, t
    assert abs(t["netGapPct"] - (t["fillGapPct"] + t["costGapPct"])) < 1e-9, t       # net gap = fill gap + cost gap
    assert abs(t["fillGapPct"] - (-0.5)) < 1e-9, t                                   # only the stop slipped: -0.5 % of notional
    assert abs(r["stopFactor"]["median"] - 1.5) < 1e-9 and abs(r["targetFactor"]["median"] - 1.0) < 1e-9, r
    assert set(r["byExit"]) == {"tp", "sl", "other"}
    return True, "net gap = fill gap + cost gap, stop factor 1.5, target factor 1.0"


def unscored_rows_stay_out():
    a = new_diff()
    diff_note(a, row("sl-unverified-exit", -0.01, -0.0112), 0.0012)
    diff_note(a, row("tp-downtime", 0.02, 0.0188), 0.0012)
    diff_note(a, {"reason": "sl", "pnl_pct": -0.01}, 0.0012)                        # no net result: unknown, never a guess
    r = diff_report(a)
    assert r["closes"] == 0 and r["unknown"] == 3, r
    assert bucket_of("tp") == "tp" and bucket_of("sl") == "sl" and bucket_of("trail-sl") == "other"
    return True, "unverified, downtime and incomplete rows are in no sum"


def finite_pf_above_99_has_losses():
    a = new_diff()
    diff_note(a, row("time", .0101, .01), .0001)
    diff_note(a, row("time", 0, -.0001), .0001)
    t = diff_report(a)["total"]
    assert t["livePf"] == 100 and t["simPf"] == 100, t
    assert t["liveNoLoss"] is False and t["simNoLoss"] is False, t
    b = new_diff()
    diff_note(b, row("time", .0101, .01), .0001)
    t = diff_report(b)["total"]
    assert t["liveNoLoss"] is True and t["simNoLoss"] is True, t
    return True, "finite PF 100 retains its loss; loss-free results have an explicit state"


def x02_report_keeps_only_owned_observation_closes():
    symbol = "BTC-USDT"
    base = row("tp", .005, .004, tp=.005, extra=dict(
        t=110, conn=CONN, symbol=symbol, client_id="G1234example", ours=True, set_id="general:L:1m:sl1:st5"))
    trail = {**base, "reason": "sl", "pnl_pct": -.012, "net_pnl_pct": -.013,
             "set_id": "general:S:1m:tr0.80:0.80"}
    rows = [base, trail, {**base, "t": 99}, {**base, "t": 121}, {**base, "ours": False},
            {**base, "conn": "bingx-x01"}, {**base, "client_id": "G4321example"},
            {**base, "symbol": "XRP-USDT"}, {**base, "reason": "bounded-session-cleanup"},
            {**base, "reason": "sl-unverified-exit"}]
    with TemporaryDirectory() as directory:
        path = Path(directory)
        (path / "state").mkdir()
        (path / "session.json").write_text(json.dumps(dict(
            connection=CONN, host=HOST, tag="G1234", symbols=[symbol], activeStartedAt=100, activeEndedAt=120,
            complete=True, accountRates={"paperCostPct": .11})))
        (path / "state" / f"trades-{CONN}.jsonl").write_text("\n".join(map(json.dumps, rows)))
        output = StringIO()
        with redirect_stdout(output):
            assert main(["--session", str(path), "--json"]) == 0
    report = json.loads(output.getvalue())
    assert (report["closes"], report["unknown"], report["session"]["paperCostPct"]) == (2, 1, .11), report
    assert report["byStep"]["tp"]["5"]["liveNetPct"] == .4, report
    assert report["byStep"]["trailArm"]["8"]["liveNetPct"] == -1.3, report
    assert report["byStep"]["trailGap"]["8"]["simNetPct"] == -1.11, report
    return True, "one owned TP and trailing close, with measured costs and no out-of-window or cleanup rows"


CHECKS = [("simdiff.gap-adds-up-and-factors-read-the-planned-level", gap_adds_up),
          ("simdiff.unscored-rows-stay-out", unscored_rows_stay_out),
          ("simdiff.finite-pf-above-99-has-losses", finite_pf_above_99_has_losses),
          ("simdiff.x02-report-keeps-only-owned-observation-closes", x02_report_keeps_only_owned_observation_closes)]
