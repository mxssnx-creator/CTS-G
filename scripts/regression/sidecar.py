"""Sidecar suite: pulse_http binds to loopback, refuses unauthenticated mutating POSTs, and restricts CORS.

The real request handler runs on an ephemeral loopback port. apply_control is replaced with a recorder, so nothing
reaches systemd or an engine, and the config directory is a temp dir, so no file outside the test is written.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from common import Skip  # noqa: F401
import pulse_http as h

TOKEN = "test-token-7f3a"


class _Server:
    """Context manager: the handler on 127.0.0.1:0 with the token, CORS list and file target patched for one check."""

    def __init__(self, token=TOKEN, origins=(), config_dir=None):
        self.token, self.origins, self.config_dir = token, set(origins), config_dir

    def __enter__(self):
        self.saved = (h.HTTP_TOKEN, h.CORS_ORIGINS, h.apply_control, h.DIR, h.load_overlay)
        self.calls = []
        h.HTTP_TOKEN = self.token
        h.CORS_ORIGINS = self.origins
        h.apply_control = lambda conn, action: (self.calls.append((conn, action)), (True, "recorded"))[1]
        if self.config_dir:
            h.DIR = self.config_dir
            h.load_overlay = lambda conn: {}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), h.Handler)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()
        h.HTTP_TOKEN, h.CORS_ORIGINS, h.apply_control, h.DIR, h.load_overlay = self.saved
        return False

    def url(self, path):
        return f"http://127.0.0.1:{self.srv.server_address[1]}{path}"

    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "text/plain", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)

    def options(self, origin=None):
        req = urllib.request.Request(self.url("/control.json"), method="OPTIONS",
                                     headers={"Origin": origin} if origin else {})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers)


def default_bind_is_loopback():
    if os.environ.get("PULSE_HTTP_HOST") not in (None, "", "127.0.0.1"):
        raise Skip("PULSE_HTTP_HOST is set in this environment; the default is checked on a clean environment")
    return h.HTTP_HOST == "127.0.0.1", f"bind={h.HTTP_HOST}:{h.HTTP_PORT}"


def control_refused_without_token():
    with _Server() as s:
        status, body, _ = s.post("/control.json?conn=vst", {"action": "stop"})
    ok = status == 403 and not s.calls and "X-Pulse-Token" in body.get("detail", "")
    return ok, f"status={status} engine_calls={len(s.calls)} detail={body.get('detail')}"


def control_refused_with_wrong_token():
    with _Server() as s:
        status, body, _ = s.post("/control.json?conn=vst", {"action": "stop"}, {"X-Pulse-Token": "guess"})
    return status == 403 and not s.calls, f"status={status} engine_calls={len(s.calls)}"


def control_accepted_with_token():
    with _Server() as s:
        status, body, _ = s.post("/control.json?conn=vst", {"action": "stop"}, {"X-Pulse-Token": TOKEN})
    # "vst" resolves to the connection id; the action reaches the engine control once
    ok = status == 200 and body.get("ok") is True and len(s.calls) == 1 and s.calls[0][1] == "stop"
    return ok, f"status={status} calls={s.calls}"


def token_unset_refuses_every_post():
    with _Server(token="") as s:
        status, body, _ = s.post("/control.json?conn=vst", {"action": "stop"}, {"X-Pulse-Token": ""})
    ok = status == 403 and not s.calls and "not set" in body.get("detail", "")
    return ok, f"status={status} detail={body.get('detail')}"


def cors_is_not_a_wildcard():
    with _Server(origins=["http://127.0.0.1:3102"]) as s:
        _, unlisted = s.options("http://evil.example")
        _, listed = s.options("http://127.0.0.1:3102")
    ok = ("Access-Control-Allow-Origin" not in unlisted
          and listed.get("Access-Control-Allow-Origin") == "http://127.0.0.1:3102")
    return ok, f"unlisted_acao={unlisted.get('Access-Control-Allow-Origin')} listed_acao={listed.get('Access-Control-Allow-Origin')}"


def config_save_is_atomic_and_leaves_no_temp_file():
    with tempfile.TemporaryDirectory() as td:
        with _Server(config_dir=td) as s:
            status, body, _ = s.post("/config.json?conn=vst", {"overlay": {"slToTpRatio": 0.9}}, {"X-Pulse-Token": TOKEN})
        written = [n for n in os.listdir(td) if n.startswith("overlay-") and n.endswith(".json")]
        saved = json.load(open(os.path.join(td, written[0]))) if len(written) == 1 else None
        leftovers = [n for n in os.listdir(td) if n.endswith(".tmp")]
    ok = status == 200 and saved == {"slToTpRatio": 0.9} and not leftovers
    return ok, f"status={status} written={written} saved={saved} temp_leftovers={leftovers}"


def stale_engine_is_not_running():
    """A stats file that stopped moving (older than 20 s) reports running=false, in the stamp and in the lane
    summary. A fresh file that says running keeps running=true."""
    saved = (h.stats_age, h.unit_state, h.load_stats, h.DIR)
    try:
        h.unit_state = lambda conn, fresh=False: "active"
        h.load_stats = lambda conn: {"running": True, "halted": False, "closed": []}
        with tempfile.TemporaryDirectory() as td:
            h.DIR = td
            lane = dict(h.LANES[0])
            h.stats_age = lambda conn: 60.0
            stale_stamp = h.stamp_stats({"running": True, "closed": []}, lane["id"])
            stale_lane = h.lane_summary(lane)
            h.stats_age = lambda conn: 2.0
            fresh_stamp = h.stamp_stats({"running": True, "closed": []}, lane["id"])
            fresh_lane = h.lane_summary(lane)
    finally:
        h.stats_age, h.unit_state, h.load_stats, h.DIR = saved
    ok = (stale_stamp["running"] is False and stale_stamp["stale"] is True
          and stale_lane["running"] is False and stale_lane["stale"] is True
          and fresh_stamp["running"] is True and fresh_lane["running"] is True and fresh_lane["stale"] is False)
    return ok, (f"stale: stamp={stale_stamp['running']} lane={stale_lane['running']}; "
                f"fresh: stamp={fresh_stamp['running']} lane={fresh_lane['running']}")


def overall_view_has_no_fake_zeros():
    """The overall view computes what the lanes report and says null for the rest. usedMargin is the sum only when
    both lanes report it; pnlPct comes from the lanes' session PnL over their equity; drawdownPct and maxOpen have no
    account-wide meaning and are null; symbols is the sorted set of open symbols across both lanes."""
    saved = (h.stats_age, h.unit_state, h.load_stats, h.DIR)

    def overall(stats_by_conn):
        h.unit_state = lambda conn, fresh=False: "active"
        h.stats_age = lambda conn: 1.0
        h.load_stats = lambda conn: dict(stats_by_conn.get(conn) or {})
        return h.merge_overall()

    def pos(sym):
        return {"symbol": sym, "side": "long", "connection": "x"}

    try:
        with tempfile.TemporaryDirectory() as td:
            h.DIR = td
            both = overall({
                "bingx-x01": {"running": True, "equity": 100.0, "sessionPnl": 2.0, "usedMargin": 10.0,
                              "open": [pos("BTC-USDT")], "closed": []},
                "bingx-x02": {"running": True, "equity": 300.0, "sessionPnl": -1.0, "usedMargin": 5.0,
                              "open": [pos("ETH-USDT"), pos("BTC-USDT")], "closed": []},
            })
            one = overall({
                "bingx-x01": {"running": True, "equity": 100.0, "sessionPnl": 2.0, "usedMargin": 10.0,
                              "open": [], "closed": []},
                "bingx-x02": {"running": True, "equity": 300.0, "sessionPnl": -1.0, "open": [], "closed": []},
            })
            empty = overall({})
    finally:
        h.stats_age, h.unit_state, h.load_stats, h.DIR = saved

    ok = (both["usedMargin"] == 15.0
          and both["pnlPct"] == 0.25
          and both["drawdownPct"] is None and both["maxOpen"] is None
          and both["symbols"] == ["BTC-USDT", "ETH-USDT"]
          and one["usedMargin"] is None and one["pnlPct"] == 0.25
          and empty["usedMargin"] is None and empty["pnlPct"] is None)
    return ok, (f"both: usedMargin={both['usedMargin']} pnlPct={both['pnlPct']} symbols={both['symbols']}; "
                f"one-lane: usedMargin={one['usedMargin']}; empty: pnlPct={empty['pnlPct']}")


CHECKS = [
    ("sidecar.default-bind-is-loopback", default_bind_is_loopback),
    ("sidecar.control-refused-without-token", control_refused_without_token),
    ("sidecar.control-refused-with-wrong-token", control_refused_with_wrong_token),
    ("sidecar.control-accepted-with-token", control_accepted_with_token),
    ("sidecar.token-unset-refuses-every-post", token_unset_refuses_every_post),
    ("sidecar.cors-is-not-a-wildcard", cors_is_not_a_wildcard),
    ("sidecar.config-save-atomic", config_save_is_atomic_and_leaves_no_temp_file),
    ("sidecar.stale-engine-is-not-running", stale_engine_is_not_running),
    ("sidecar.overall-view-has-no-fake-zeros", overall_view_has_no_fake_zeros),
]


if __name__ == "__main__":
    bad = 0
    for name, fn in CHECKS:
        ok, detail = fn()
        bad += 0 if ok else 1
        print(("PASS " if ok else "FAIL ") + name, detail)
    raise SystemExit(1 if bad else 0)
