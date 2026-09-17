#!/usr/bin/env python3
"""2-hour remote Live mainnet monitor. Never flattens positions. Never arms 8581.

Keeps bingx-x01 on mainnet with Block enabled across every indication relation,
Set family, and Overall. Repairs the 1-second prehistoric timeframe bug that
left the catalog empty (0 candles → 0 live parents → 0 Block stacks).
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://152.53.114.112:3002"
LIVE_ID = "bingx-x01"
VST_ID = "bingx-x02"
FORBIDDEN = "bingx-8581b0cb8581"
OUT = Path("/workspace/reports/live-monitor-2h")
OUT.mkdir(parents=True, exist_ok=True)
LOG = OUT / "monitor.log"
JSONL = OUT / "snapshots.jsonl"
MINUTES = 120
INTERVAL_S = 300

SYMBOLS_50 = [
    "BTCUSDT", "SOLUSDT", "BCHUSDT", "XRPUSDT", "ETHUSDT", "BNBUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT", "UNIUSDT", "ATOMUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "SUIUSDT", "INJUSDT", "TIAUSDT", "AAVEUSDT",
    "WLDUSDT", "FILUSDT", "OPUSDT", "TRXUSDT", "XLMUSDT", "ETCUSDT", "JUPUSDT",
    "RENDERUSDT", "FETUSDT", "TAOUSDT", "SEIUSDT", "WIFUSDT", "PEPEUSDT", "LDOUSDT",
    "STXUSDT", "IMXUSDT", "GRTUSDT", "HBARUSDT", "ALGOUSDT", "VETUSDT", "EOSUSDT",
    "THETAUSDT", "AXSUSDT", "SANDUSDT", "MANAUSDT", "CRVUSDT", "MKRUSDT", "SNXUSDT",
    "COMPUSDT",
]

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


def req(method, path, body=None, timeout=25):
    data = None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
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


def log(line: str) -> None:
    row = f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {line}"
    print(row, flush=True)
    LOG.write_text((LOG.read_text() if LOG.exists() else "") + row + "\n")


def _int(v, default=0) -> int:
    try:
        if v is True:
            return 1
        if v is False or v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def block_payload(epoch: str | None = None) -> dict:
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
        "blockRowLiveIncrementSteps": 1,
        "presetBlockIncrementSteps": 1,
        "blockVolumeRatio": 1,
        "blockRowLiveVolumeRatio": 1,
        "blockProfitFactorRatio": 1.1,
        "blockRowLiveProfitFactorRatio": 1.1,
        "blockPauseCountRatio": 1,
        "blockRowLivePauseCountRatio": 1,
        "blockRowRealEvalPosCount": 50,
        "blockEvalPosCount": 50,
        "setUseHistoricGate": False,
        "entry_processors_gated": 0,
        "strategyLiveSetsCeiling": 0,
        "strategyRealSetsSafetyCeiling": 0,
        "liveProfitFactor": 1.1,
        "mainProfitFactor": 1.1,
        "realProfitFactor": 1.1,
        "live_min_profit_factor": 1.1,
        "timeframeSeconds": 60,
        "candleTimeframeSeconds": 60,
        "histTimeframeSeconds": 60,
        "prehistoricTimeframeSeconds": 60,
        "prehistoric_timeframe_seconds": 60,
        "tradeIntervalSeconds": 60,
        "mainTradeInterval": 60,
        "indication_time_interval": 60,
        "strategy_time_interval": 60,
        "klineInterval": "1m",
        "interval": "1m",
        "maxPositionsLong": 500,
        "maxPositionsShort": 500,
        "maxConcurrentOperations": 100,
        "signal_trade_enabled": True,
        "is_signal_trade": True,
    }
    payload.update(BLOCK_RELATION_FLAGS)
    return payload


def apply_block_and_timeframe(*, rerun_prehistoric: bool) -> dict:
    epoch = f"{int(time.time() * 1000)}:blockfix" if rerun_prehistoric else None
    payload = block_payload(epoch)
    results = {}
    results["put_conn"] = req("PUT", f"/api/settings/connections/{LIVE_ID}", payload)[0]
    results["patch_settings"] = req("PATCH", f"/api/settings/connections/{LIVE_ID}/settings", payload)[0]
    results["put_settings"] = req("PUT", f"/api/settings/connections/{LIVE_ID}/settings", payload)[0]
    results["post_global"] = None
    results["live_trade"] = req(
        "POST",
        f"/api/settings/connections/{LIVE_ID}/live-trade",
        {"is_live_trade": True, "is_testnet": False},
    )[0]
    results["enable"] = req(
        "POST",
        f"/api/settings/connections/{LIVE_ID}/enable",
        {"enabled": True, "exchange": "bingx"},
    )[0]
    results["active"] = req(
        "POST",
        f"/api/settings/connections/{LIVE_ID}/active",
        {"is_active": True, "exchange": "bingx"},
    )[0]
    if rerun_prehistoric:
        results["quick_start"] = req(
            "POST",
            "/api/trade-engine/quick-start",
            {"action": "start", "connectionId": LIVE_ID, "exchange": "bingx"},
        )[0]
        results["engine_start"] = req(
            "POST",
            "/api/trade-engine/start",
            {"connectionId": LIVE_ID},
        )[0]
    log(f"repair http={results} epoch={epoch} prehistoric={rerun_prehistoric}")
    return results


def disable_forbidden() -> None:
    req("POST", f"/api/settings/connections/{FORBIDDEN}/live-trade", {"is_live_trade": False})
    req("POST", f"/api/settings/connections/{FORBIDDEN}/enable", {"enabled": False})
    req("POST", "/api/trade-engine/quick-start", {"action": "disable", "connectionId": FORBIDDEN})


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
    live = (es or {}).get("live") if isinstance(es, dict) else {}
    enabled = (es or {}).get("enabled") if isinstance(es, dict) else {}
    p = (prog or {}).get("progression") if isinstance(prog, dict) else {}
    metrics = (prog or {}).get("metrics") if isinstance(prog, dict) else {}
    rows = (pos or {}).get("data") if isinstance(pos, dict) else []
    open_rows = [r for r in rows if str((r or {}).get("status") or "").lower() == "open"]
    te_live = next((c for c in ((te or {}).get("connections") or []) if c.get("id") == LIVE_ID), {}) if isinstance(te, dict) else {}
    te_bad = next((c for c in ((te or {}).get("connections") or []) if c.get("id") == FORBIDDEN), None) if isinstance(te, dict) else None
    settings = (nested or {}).get("settings") if isinstance(nested, dict) else {}
    cs = (x01 or {}).get("connection_settings") if isinstance(x01, dict) else {}
    if isinstance(cs, str):
        try:
            cs = json.loads(cs)
        except Exception:
            cs = {}
    tf = _int((settings or {}).get("prehistoric_timeframe_seconds"), -1)
    candles = _int((metrics or {}).get("prehistoricCandlesProcessed"), 0)
    strat_pos = _int((metrics or {}).get("prehistoricStrategyPositions"), 0)
    strategies = _int((metrics or {}).get("strategiesCount"), 0)
    block_flags = {
        k: bool((x01 or {}).get(k) is True or str((cs or {}).get(k)).lower() == "true")
        for k in (
            "strategyBlockEnabled",
            "strategyDirectionBlockEnabled",
            "strategyMoveBlockEnabled",
            "strategyActiveBlockEnabled",
            "strategyTrendBlockEnabled",
            "strategyBreakBlockEnabled",
            "strategyCommonBlockEnabled",
            "strategySignalBlockEnabled",
            "blockEnabled",
            "variantBlockEnabled",
            "blockActiveLiveEnabled",
            "blockActiveRealEnabled",
            "blockOverall",
        )
    }
    vst_m = (vst or {}).get("metrics") if isinstance(vst, dict) else {}
    blob = {
        "pass": pass_n,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "http": {
            "engine": st_es, "progression": st_p, "status": st_te, "health": st_h,
            "positions": st_pos, "orders": st_ord,
        },
        "running": bool(isinstance(es, dict) and es.get("engineRunning")),
        "liveMode": (live or {}).get("executionMode"),
        "liveEffective": (live or {}).get("effective"),
        "liveBlock": (live or {}).get("blockCode"),
        "liveReason": (live or {}).get("blockReason"),
        "enabled": (enabled or {}).get("flag"),
        "phase": (p or {}).get("phase"),
        "prehistoricFilling": str((p or {}).get("phase") or "").startswith("prehistoric"),
        "progress": (p or {}).get("progress"),
        "message": (p or {}).get("message"),
        "liveTradingActive": ((p or {}).get("details") or {}).get("liveTradingActive"),
        "openCount": len(open_rows),
        "orderCount": (orders or {}).get("count") if isinstance(orders, dict) else None,
        "positionRows": len(rows or []),
        "teOpen": te_live.get("openPositions"),
        "teOrders": te_live.get("openOrders"),
        "tePositions": te_live.get("positions"),
        "teTrades": te_live.get("trades"),
        "healthOpen": ((health or {}).get("system") or {}).get("totalOpenPositions") if isinstance(health, dict) else None,
        "isTestnet": (x01 or {}).get("is_testnet") if isinstance(x01, dict) else None,
        "blockOnlyEnabled": bool((x01 or {}).get("blockOnlyEnabled")) if isinstance(x01, dict) else None,
        "csBlockOnlyEnabled": (cs or {}).get("blockOnlyEnabled") if isinstance(cs, dict) else None,
        "blockFlagsOn": sum(1 for v in block_flags.values() if v),
        "blockFlags": block_flags,
        "blockMaxStack": (x01 or {}).get("blockMaxStack") if isinstance(x01, dict) else None,
        "prehistoricTf": tf,
        "prehistoricCandles": candles,
        "prehistoricStrategyPositions": strat_pos,
        "strategiesCount": strategies,
        "strategyEvaluatedReal": (metrics or {}).get("strategyEvaluatedReal"),
        "lastStrategyRun": (metrics or {}).get("lastStrategyRun"),
        "symbolsWithoutData": ((p or {}).get("prehistoricProgress") or {}).get("configWork"),
        "forbiddenRunning": bool(isinstance(es8, dict) and es8.get("engineRunning")),
        "forbiddenPresent": te_bad is not None,
        "vstOpen": (vst_m or {}).get("openPositions") if False else None,
        "vstStrategies": (vst_m or {}).get("strategiesCount"),
        "openSymbols": [
            {
                "symbol": r.get("symbol"),
                "side": r.get("side") or r.get("direction"),
                "qty": r.get("quantity") or r.get("executedQuantity"),
                "sl": r.get("stopLossPrice"),
                "tp": r.get("takeProfitPrice"),
                "setKey": str(r.get("setKey") or "")[:80],
                "blockCount": r.get("blockCount"),
            }
            for r in open_rows[:24]
        ],
        "needsPrehistoric": tf in (1, -1),
        "needsBlock": any(v is False for v in block_flags.values()) or bool((x01 or {}).get("blockOnlyEnabled")),
    }
    with JSONL.open("a") as f:
        f.write(json.dumps(blob, default=str) + "\n")
    (OUT / "latest.json").write_text(json.dumps(blob, indent=2, default=str))
    return blob


def repair_if_needed(pass_n: int, blob: dict, *, force: bool = False) -> dict:
    repaired = False
    filling = str(blob.get("phase") or "").startswith("prehistoric") or blob.get("prehistoricFilling")
    if blob.get("forbiddenRunning"):
        log(f"pass {pass_n} FORBIDDEN 8581 running — disabling")
        disable_forbidden()
        repaired = True
    if blob.get("isTestnet") is True:
        log(f"pass {pass_n} drifted to testnet — forcing mainnet")
        apply_block_and_timeframe(rerun_prehistoric=False)
        repaired = True
    if blob.get("liveEffective") is False and not filling:
        log(f"pass {pass_n} live not effective — re-arming mainnet + Block")
        apply_block_and_timeframe(rerun_prehistoric=False)
        req("POST", "/api/trade-engine/start", {"connectionId": LIVE_ID})
        repaired = True
    elif blob.get("running") is False and not filling:
        log(f"pass {pass_n} engine heartbeat down — starting without settings reshuffle")
        req("POST", "/api/trade-engine/start", {"connectionId": LIVE_ID})
        req("POST", "/api/trade-engine/quick-start", {"action": "start", "connectionId": LIVE_ID, "exchange": "bingx"})
        repaired = True
    if (blob.get("needsBlock") or blob.get("blockOnlyEnabled") or blob.get("csBlockOnlyEnabled")) and not filling:
        log(f"pass {pass_n} Block relations incomplete — enabling Block on all Sets/Overall")
        apply_block_and_timeframe(rerun_prehistoric=False)
        repaired = True
    if blob.get("needsPrehistoric") and not filling:
        log(
            f"pass {pass_n} prehistoric empty/tf={blob.get('prehistoricTf')} "
            f"candles={blob.get('prehistoricCandles')} — rerunning 1m bootstrap"
        )
        apply_block_and_timeframe(rerun_prehistoric=True)
        repaired = True
    elif filling:
        log(
            f"pass {pass_n} prehistoric filling phase={blob.get('phase')} "
            f"candles={blob.get('prehistoricCandles')} — waiting"
        )
    return snapshot(pass_n) if repaired else blob


def main() -> int:
    log(f"start live={LIVE_ID} minutes={MINUTES} interval={INTERVAL_S}s block-all-relations")
    disable_forbidden()
    first = snapshot(0)
    first = repair_if_needed(0, first, force=False)
    log(
        f"bootstrap running={first.get('running')} live={first.get('liveEffective')} "
        f"tf={first.get('prehistoricTf')} candles={first.get('prehistoricCandles')} "
        f"strats={first.get('strategiesCount')} open={first.get('openCount')} "
        f"blockFlags={first.get('blockFlagsOn')}"
    )
    passes = max(1, int(MINUTES * 60 / INTERVAL_S) + 1)
    for i in range(1, passes + 1):
        blob = snapshot(i)
        blob = repair_if_needed(i, blob, force=False)
        log(
            f"pass {i}/{passes} running={blob.get('running')} mode={blob.get('liveMode')} "
            f"phase={blob.get('phase')} liveActive={blob.get('liveTradingActive')} "
            f"open={blob.get('openCount')} teOpen={blob.get('teOpen')} teOrders={blob.get('teOrders')} "
            f"tf={blob.get('prehistoricTf')} candles={blob.get('prehistoricCandles')} "
            f"stratPos={blob.get('prehistoricStrategyPositions')} strats={blob.get('strategiesCount')} "
            f"blockFlags={blob.get('blockFlagsOn')} blockOnly={blob.get('blockOnlyEnabled')} "
            f"testnet={blob.get('isTestnet')} 8581={blob.get('forbiddenRunning')}"
        )
        if i < passes:
            time.sleep(INTERVAL_S)
    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
