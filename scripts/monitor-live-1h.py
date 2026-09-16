#!/usr/bin/env python3
"""1-hour remote Live + VST monitor. Never flattens. Never arms 8581. Never posts symbol lists."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("CTS_MONITOR_BASE", "http://152.53.114.112:3002").rstrip("/")
PULSE = os.environ.get("CTS_MONITOR_PULSE", "http://152.53.114.112:3015").rstrip("/")
LIVE_ID = "bingx-x01"
VST_ID = "bingx-x02"
FORBIDDEN = "bingx-8581b0cb8581"
OUT = Path(os.environ.get("CTS_MONITOR_OUT", "/workspace/reports/live-monitor-1h"))
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "monitor.log"
JSONL = OUT / "snapshots.jsonl"
MINUTES = 60
INTERVAL_S = 180
MAX_LOG_LINES = 400
MAX_JSONL_LINES = 80

BLOCK_RELATION_FLAGS = {
    "strategyBlockEnabled": True,
    "strategyDirectionBlockEnabled": True,
    "strategyMoveBlockEnabled": True,
    "strategyActiveBlockEnabled": True,
    "strategyActiveAdvancedBlockEnabled": True,
    "strategySignalBlockEnabled": True,
    "strategyTrendBlockEnabled": True,
    "strategyBreakBlockEnabled": True,
    "strategyCommonBlockEnabled": True,
    "strategyOptimalBlockEnabled": True,
    "strategySpecialBlockEnabled": True,
    "strategyAutoBlockEnabled": True,
    "exchangeBlockEnabled": True,
    "presetBlockEnabled": True,
    "presetBlockStrategy": True,
    "presetBlockActiveLiveEnabled": True,
    "presetBlockActiveRealEnabled": True,
    "blockActiveLiveEnabled": True,
    "blockActiveRealEnabled": True,
    "blockRowLiveEnabled": True,
    "variantBlockEnabled": True,
    "blockEnabled": True,
    "blockAdjustment": True,
    "block_enabled": True,
    "blockOverall": True,
    "variant_block": True,
}


def req(method, path, body=None, timeout=25, base=BASE):
    data = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    r = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw[:1200]
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode()
        except Exception:
            raw = str(e)
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:1200]
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def retain_lines(path: Path, line: str, max_lines: int) -> None:
    prev = path.read_text() if path.exists() else ""
    rows = [row for row in prev.splitlines() if row][-max(0, max_lines - 1):]
    rows.append(line.rstrip("\n"))
    path.write_text("\n".join(rows) + "\n")


def log(line: str) -> None:
    row = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {line}"
    print(row, flush=True)
    retain_lines(LOG, row, MAX_LOG_LINES)


def _int(v, default=0) -> int:
    try:
        if v is True:
            return 1
        if v is False or v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def block_payload() -> dict:
    payload = {
        "is_testnet": False,
        "is_live_trade": True,
        "live_trade_enabled": True,
        "live_trade_requested": True,
        "is_enabled": True,
        "is_active": True,
        "exchange": "bingx",
        "useMaximalLeverage": True,
        "useControlOrders": True,
        "controlOrdersEnabled": True,
        "normalEnabled": True,
        "blockOnly": False,
        "variantBlockOnly": False,
        "blockOnlyEnabled": False,
        "block_only_enabled": False,
        "strategyBlockOnlyEnabled": False,
        "blockMaxStack": 6,
        "blockRowLiveMaxStack": 6,
        "presetBlockMaxStack": 6,
        "blockIncrementSteps": 1,
        "blockVolumeRatio": 1,
        "blockProfitFactorRatio": 1.1,
        "blockPauseCountRatio": 1,
        "setUseHistoricGate": False,
        "entry_processors_gated": 0,
        "strategyLiveSetsCeiling": 0,
        "strategyRealSetsSafetyCeiling": 0,
        "liveProfitFactor": 1.1,
        "mainProfitFactor": 1.1,
        "realProfitFactor": 1.1,
        "timeframeSeconds": 60,
        "candleTimeframeSeconds": 60,
        "histTimeframeSeconds": 60,
        "prehistoricTimeframeSeconds": 60,
        "prehistoric_timeframe_seconds": 60,
        "klineInterval": "1m",
        "maxConcurrentOperations": 100,
        "signal_trade_enabled": True,
        "is_signal_trade": True,
        "histTestEnabled": True,
        "symbolCap": 50,
        "maxOpen": 100,
    }
    payload.update(BLOCK_RELATION_FLAGS)
    return payload


def apply_live_repair() -> dict:
    payload = block_payload()
    results = {
        "put_conn": req("PUT", f"/api/settings/connections/{LIVE_ID}", payload)[0],
        "patch_settings": req("PATCH", f"/api/settings/connections/{LIVE_ID}/settings", payload)[0],
        "live_trade": req("POST", f"/api/settings/connections/{LIVE_ID}/live-trade", {"is_live_trade": True, "is_testnet": False})[0],
        "enable": req("POST", f"/api/settings/connections/{LIVE_ID}/enable", {"enabled": True, "exchange": "bingx"})[0],
        "active": req("POST", f"/api/settings/connections/{LIVE_ID}/active", {"is_active": True, "exchange": "bingx"})[0],
        "engine_start": req("POST", "/api/trade-engine/start", {"connectionId": LIVE_ID})[0],
    }
    log(f"repair live http={results}")
    return results


def disable_forbidden() -> None:
    req("POST", f"/api/settings/connections/{FORBIDDEN}/live-trade", {"is_live_trade": False})
    req("POST", f"/api/settings/connections/{FORBIDDEN}/enable", {"enabled": False})
    req("POST", "/api/trade-engine/quick-start", {"action": "disable", "connectionId": FORBIDDEN})


def pulse_lane(conn: str) -> dict:
    st, body = req("GET", f"/stats/{conn}.json", timeout=12, base=PULSE)
    if st != 200 or not isinstance(body, dict):
        st, body = req("GET", f"/live-stats.json?id={conn}", timeout=12, base=PULSE)
    if not isinstance(body, dict):
        return {"http": st, "detail": str(body)[:180]}
    ht = body.get("histTest") if isinstance(body.get("histTest"), dict) else {}
    sets = body.get("sets") if isinstance(body.get("sets"), dict) else {}
    return {
        "http": st,
        "running": body.get("running"),
        "openCount": body.get("openCount"),
        "mode": body.get("mode"),
        "symbolCount": body.get("symbolCount") or (len(body.get("symbols") or []) if isinstance(body.get("symbols"), list) else None),
        "validatedCount": sets.get("validatedCount"),
        "activeCount": sets.get("activeCount"),
        "processingCount": sets.get("processingCount"),
        "histTestEnabled": ht.get("enabled"),
        "histTestPhase": ht.get("phase"),
        "histTestSets": len(ht.get("runningSets") or []),
        "internSymbols": len(ht.get("internSymbols") or ht.get("symbols") or []),
        "detail": str(ht.get("detail") or body.get("progressDetail") or "")[:160],
        "rssMb": ((body.get("system") or {}) if isinstance(body.get("system"), dict) else {}).get("rssMb"),
    }


def snapshot(pass_n: int) -> dict:
    st_es, es = req("GET", f"/api/connections/{LIVE_ID}/engine-states")
    st_p, prog = req("GET", f"/api/connections/progression/{LIVE_ID}")
    st_te, te = req("GET", "/api/trade-engine/status")
    st_h, health = req("GET", "/api/health")
    st_pos, pos = req("GET", f"/api/positions?connection_id={LIVE_ID}")
    st_ord, orders = req("GET", f"/api/orders?connection_id={LIVE_ID}")
    st_x, x01 = req("GET", f"/api/settings/connections/{LIVE_ID}")
    st_xs, nested = req("GET", f"/api/settings/connections/{LIVE_ID}/settings")
    st_8, es8 = req("GET", f"/api/connections/{FORBIDDEN}/engine-states")
    st_v, vst = req("GET", f"/api/connections/progression/{VST_ID}")
    st_vpos, vpos = req("GET", f"/api/positions?connection_id={VST_ID}")
    live = (es or {}).get("live") if isinstance(es, dict) else {}
    enabled = (es or {}).get("enabled") if isinstance(es, dict) else {}
    p = (prog or {}).get("progression") if isinstance(prog, dict) else {}
    metrics = (prog or {}).get("metrics") if isinstance(prog, dict) else {}
    rows = (pos or {}).get("data") if isinstance(pos, dict) else []
    open_rows = [r for r in rows if str((r or {}).get("status") or "").lower() == "open"]
    vrows = (vpos or {}).get("data") if isinstance(vpos, dict) else []
    vopen = [r for r in vrows if str((r or {}).get("status") or "").lower() == "open"]
    te_live = next((c for c in ((te or {}).get("connections") or []) if c.get("id") == LIVE_ID), {}) if isinstance(te, dict) else {}
    te_vst = next((c for c in ((te or {}).get("connections") or []) if c.get("id") == VST_ID), {}) if isinstance(te, dict) else {}
    te_bad = next((c for c in ((te or {}).get("connections") or []) if c.get("id") == FORBIDDEN), None) if isinstance(te, dict) else None
    settings = (nested or {}).get("settings") if isinstance(nested, dict) else {}
    cs = (x01 or {}).get("connection_settings") if isinstance(x01, dict) else {}
    if isinstance(cs, str):
        try:
            cs = json.loads(cs)
        except Exception:
            cs = {}
    tf = _int((settings or {}).get("prehistoric_timeframe_seconds"), -1)
    block_flags = {
        k: bool((x01 or {}).get(k) is True or str((cs or {}).get(k)).lower() == "true")
        for k in (
            "strategyBlockEnabled", "strategyDirectionBlockEnabled", "strategyMoveBlockEnabled",
            "strategyActiveBlockEnabled", "strategyTrendBlockEnabled", "strategyBreakBlockEnabled",
            "strategyCommonBlockEnabled", "strategySignalBlockEnabled", "blockEnabled",
            "variantBlockEnabled", "blockActiveLiveEnabled", "blockActiveRealEnabled", "blockOverall",
        )
    }
    protected = sum(1 for r in open_rows if r.get("stopLossPrice") and r.get("takeProfitPrice"))
    pulse_x01 = pulse_lane(LIVE_ID)
    pulse_x02 = pulse_lane(VST_ID)
    blob = {
        "pass": pass_n,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "http": {"engine": st_es, "progression": st_p, "status": st_te, "health": st_h, "positions": st_pos, "orders": st_ord},
        "running": bool(isinstance(es, dict) and es.get("engineRunning")),
        "liveMode": (live or {}).get("executionMode"),
        "liveEffective": (live or {}).get("effective"),
        "liveBlock": (live or {}).get("blockCode"),
        "liveReason": str((live or {}).get("blockReason") or "")[:160],
        "enabled": (enabled or {}).get("flag"),
        "phase": (p or {}).get("phase"),
        "openCount": len(open_rows),
        "protectedCount": protected,
        "orderCount": (orders or {}).get("count") if isinstance(orders, dict) else None,
        "teOpen": te_live.get("openPositions"),
        "teOrders": te_live.get("openOrders"),
        "vstOpen": len(vopen),
        "vstTeOpen": te_vst.get("openPositions"),
        "isTestnet": (x01 or {}).get("is_testnet") if isinstance(x01, dict) else None,
        "blockFlagsOn": sum(1 for v in block_flags.values() if v),
        "blockMaxStack": (x01 or {}).get("blockMaxStack") if isinstance(x01, dict) else None,
        "prehistoricTf": tf,
        "strategiesCount": _int((metrics or {}).get("strategiesCount"), 0),
        "forbiddenRunning": bool(isinstance(es8, dict) and es8.get("engineRunning")),
        "forbiddenPresent": te_bad is not None,
        "pulseX01": pulse_x01,
        "pulseX02": pulse_x02,
        "needsPrehistoric": tf in (1, -1),
        "needsBlock": any(v is False for v in block_flags.values()) or bool((x01 or {}).get("blockOnlyEnabled")),
        "openSymbols": [
            {
                "symbol": r.get("symbol"),
                "side": r.get("side") or r.get("direction"),
                "sl": bool(r.get("stopLossPrice")),
                "tp": bool(r.get("takeProfitPrice")),
            }
            for r in open_rows[:24]
        ],
    }
    retain_lines(JSONL, json.dumps(blob, default=str), MAX_JSONL_LINES)
    (OUT / "latest.json").write_text(json.dumps(blob, indent=2, default=str)[:120000])
    return blob


def repair_if_needed(pass_n: int, blob: dict) -> dict:
    repaired = False
    filling = str(blob.get("phase") or "").startswith("prehistoric")
    if blob.get("forbiddenRunning"):
        log(f"pass {pass_n} FORBIDDEN 8581 running — disabling")
        disable_forbidden()
        repaired = True
    if blob.get("isTestnet") is True:
        log(f"pass {pass_n} drifted to testnet — forcing mainnet")
        apply_live_repair()
        repaired = True
    if blob.get("liveEffective") is False and not filling:
        log(f"pass {pass_n} live not effective — re-arming mainnet + Block")
        apply_live_repair()
        repaired = True
    elif blob.get("running") is False and not filling:
        log(f"pass {pass_n} engine heartbeat down — starting Live")
        req("POST", "/api/trade-engine/start", {"connectionId": LIVE_ID})
        repaired = True
    if blob.get("needsBlock") and not filling:
        log(f"pass {pass_n} Block relations incomplete — enabling Block on all Sets/Overall")
        apply_live_repair()
        repaired = True
    if blob.get("needsPrehistoric") and not filling:
        log(f"pass {pass_n} prehistoric tf={blob.get('prehistoricTf')} — patch 60s without symbol list")
        apply_live_repair()
        repaired = True
    return snapshot(pass_n) if repaired else blob


def main() -> int:
    log(f"start live={LIVE_ID} vst={VST_ID} minutes={MINUTES} interval={INTERVAL_S}s")
    disable_forbidden()
    first = repair_if_needed(0, snapshot(0))
    log(
        f"bootstrap running={first.get('running')} live={first.get('liveEffective')} "
        f"open={first.get('openCount')} protected={first.get('protectedCount')} "
        f"vstOpen={first.get('vstOpen')} tf={first.get('prehistoricTf')} "
        f"strats={first.get('strategiesCount')} blockFlags={first.get('blockFlagsOn')} "
        f"hist={((first.get('pulseX01') or {}).get('histTestPhase'))} "
        f"intern={((first.get('pulseX01') or {}).get('internSymbols'))}"
    )
    passes = max(1, int(MINUTES * 60 / INTERVAL_S) + 1)
    for i in range(1, passes + 1):
        blob = repair_if_needed(i, snapshot(i))
        px = blob.get("pulseX01") or {}
        vx = blob.get("pulseX02") or {}
        log(
            f"pass {i}/{passes} running={blob.get('running')} live={blob.get('liveEffective')} "
            f"open={blob.get('openCount')} prot={blob.get('protectedCount')} "
            f"teOpen={blob.get('teOpen')} vstOpen={blob.get('vstOpen')} "
            f"tf={blob.get('prehistoricTf')} strats={blob.get('strategiesCount')} "
            f"block={blob.get('blockFlagsOn')} testnet={blob.get('isTestnet')} "
            f"8581={blob.get('forbiddenRunning')} "
            f"x01 hist={px.get('histTestPhase')} intern={px.get('internSymbols')} "
            f"valid={px.get('validatedCount')} active={px.get('activeCount')} rss={px.get('rssMb')} "
            f"x02 hist={vx.get('histTestPhase')} open={vx.get('openCount')} rss={vx.get('rssMb')} "
            f"reason={blob.get('liveReason')}"
        )
        if i < passes:
            time.sleep(INTERVAL_S)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
