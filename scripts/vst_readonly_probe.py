"""One current VST position snapshot, never orders or settings writes.

Run with the installed CTS-G service environment. Credentials are used by the
normal connector and are never printed. Account-wide rows do not prove CTS-G
ownership, round-trip profitability, or protection by valid control orders.
"""
import json
import math
import pathlib
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse


def validate_base(base):
    parsed = urlparse(base)
    if (parsed.scheme != "https" or parsed.hostname != "open-api-vst.bingx.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("VST endpoint required")
    return base.rstrip("/")


def snapshot(api):
    body = api.get("/openApi/swap/v2/user/positions")
    if not isinstance(body, dict) or body.get("code") != 0 or not isinstance(body.get("data"), list):
        return {"ok": False, "code": body.get("code") if isinstance(body, dict) else None}
    count = 0
    symbols = set()
    for row in body["data"]:
        if not isinstance(row, dict):
            return {"ok": False, "error": "malformed_position"}
        try:
            amount = float(row.get("positionAmt") or row.get("availableAmt") or 0)
        except (ValueError, TypeError):
            return {"ok": False, "error": "malformed_quantity"}
        if not math.isfinite(amount):
            return {"ok": False, "error": "malformed_quantity"}
        if abs(amount) > 1e-12:
            count += 1
            symbols.add(str(row.get("symbol") or "unknown"))
    return {"ok": True, "source": "VST positions REST", "receivedAt": time.time(),
            "exchangeTotalOpenCount": count, "symbols": sorted(symbols),
            "ownership": "not inferred", "profitability": "not evaluated"}


def main():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server" / "pulse"))
    import pulse_trader as trader
    from bingx_fast import FastBingX
    if trader.CONN_SHORT != "bingx-x02":
        raise SystemExit("Only the configured X02 VST connection is supported")
    base = validate_base(trader.redis_hget("base_url") or "https://open-api-vst.bingx.com")
    key, secret = trader.redis_hget("api_key"), trader.redis_hget("api_secret")
    if not key or not secret:
        raise SystemExit("Missing configured X02 credentials")
    api = FastBingX(key, secret, SimpleNamespace(write=lambda *args, **kw: None), base=base)
    result = snapshot(api)
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
