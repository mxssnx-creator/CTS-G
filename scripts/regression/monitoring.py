"""Read-only monitoring checks: fake responses, no live network or trading process."""
import importlib.util
import io
import json
from pathlib import Path
import urllib.error

spec = importlib.util.spec_from_file_location("ctsg_readonly_monitor", Path(__file__).parents[1] / "monitor_readonly.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def healthy():
    return {"connection": "bingx-x01", "running": True, "alive": True, "halted": False,
            "now": 1000, "cycle": 2, "symbolCount": 50, "errors": 0, "openCount": 3,
            "sets": {"pfBase": {"base": {"pf": 3, "sets": 10, "n": 80}, "main": {"pf": 2, "sets": 8, "n": 50}}}}


def exact_get_and_no_controls():
    seen = []
    def open_(request, timeout):
        seen.append((request.get_method(), request.full_url, request.data, timeout))
        return io.BytesIO(json.dumps(healthy()).encode())
    got = m.read_stats("https://example.test", "live", opener=open_)
    return got == healthy() and seen == [("GET", "https://example.test/stats.json?conn=live", None, 8)], str(seen)


def rejects_credentials_and_control_urls():
    rejected = 0
    for url in ("https://key:secret@example.test", "https://example.test/control.json", "https://example.test?x=1", "file:///tmp/state"):
        try:
            m.read_stats(url, "live", opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
        except ValueError:
            rejected += 1
    return rejected == 4, f"{rejected} invalid targets rejected without network"


def stale_identity_symbols_and_halt():
    row = {**healthy(), "connection": "bingx-x02", "now": 800, "symbolCount": 20, "halted": True}
    got = m.HealthMonitor().sample(row, 1000)
    return not got["healthy"] and len(got["warnings"]) == 4, str(got["warnings"])


def stalls_and_recovers_without_pf_clamping():
    monitor = m.HealthMonitor()
    first = monitor.sample(healthy(), 1000)
    stalled = monitor.sample({**healthy(), "now": 1121}, 1121)
    recovered = monitor.sample({**healthy(), "now": 1122, "cycle": 3}, 1122)
    ok = first["healthy"] and not stalled["healthy"] and recovered["healthy"]
    warming = monitor.sample({**healthy(), "now": 1300, "cycle": 3,
                              "sets": {"progress": {"barsDone": 200}, "pfBase": {"base": {"pf": 99},
                                       "stages": {"base": {"pf": 3}, "main": {"pf": 2}}}}}, 1300)
    return ok and warming["healthy"] and warming["paperBase"]["pf"] == 3 and recovered["paperMain"]["pf"] == 2, "stalled cycle detected; warmup progress accepted; stage-specific PF is not clamped"


def error_increases_only():
    monitor = m.HealthMonitor()
    monitor.sample(healthy(), 1000)
    failed = monitor.sample({**healthy(), "errors": 1}, 1001)
    stable = monitor.sample({**healthy(), "errors": 1}, 1002)
    return not failed["healthy"] and stable["healthy"], "new error alert; lifetime error count alone is not an ongoing burst"


def unavailable_and_response_limit():
    def open_(*a, **k):
        raise urllib.error.HTTPError("https://example.test", 503, "unavailable", {}, None)
    unavailable = m.read_stats("https://example.test", "live", opener=open_)
    big = m.read_stats("https://example.test", "live", opener=lambda *a, **k: io.BytesIO(b"x" * (m.MAX_RESPONSE + 1)))
    got = m.HealthMonitor().sample(unavailable, 1000)
    malformed = m.HealthMonitor().sample({**healthy(), "openCount": float("nan"),
                                         "sets": {"pfBase": {"main": {"pf": float("inf")}}}}, 1000)
    json.dumps(malformed, allow_nan=False)
    return not got["healthy"] and not got["reachable"] and "exceeds" in big["_error"] and not malformed["healthy"], "HTTP 503, oversized responses and nonfinite metrics cannot report healthy or crash JSON output"


CHECKS = [("monitoring." + f.__name__.replace("_", "-"), f) for f in (
    exact_get_and_no_controls, rejects_credentials_and_control_urls, stale_identity_symbols_and_halt,
    stalls_and_recovers_without_pf_clamping, error_increases_only, unavailable_and_response_limit)]
