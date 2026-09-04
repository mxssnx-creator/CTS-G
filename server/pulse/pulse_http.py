#!/usr/bin/env python3
"""Serve per-connection stats/config. Lanes run independently; overall aggregates."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from position_cost import POSITION_COST_PCT_DEFAULT, last_n_cost_pf
from user_presets import UserPresetStore
from storage_paths import DATA_DIR, INSTANCE_NAME, append_log, atomic_write, log_path, path_for, redis_cli_args, storage_info

DIR = str(DATA_DIR)
STOP_ALL_PATH = path_for("STOP")
SYSTEMD_PREFIX = os.environ.get("CTS_SYSTEMD_PREFIX", INSTANCE_NAME)
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", SYSTEMD_PREFIX):
    raise ValueError("invalid CTS_SYSTEMD_PREFIX")
try:
    PULSE_PORT = int(os.environ.get("PULSE_PORT", "3015"))
except (TypeError, ValueError):
    PULSE_PORT = 3015
if PULSE_PORT < 1 or PULSE_PORT > 65535:
    PULSE_PORT = 3015


def pulse_unit(cid: str) -> str:
    return f"{SYSTEMD_PREFIX}-pulse@{cid}"

engine_unit = pulse_unit

# Display type → redis connection id. Independent processes write stats-{id}.json.
LANES = [
    {"type": "live", "id": "bingx-x01", "label": "Live", "unit": "USDT", "exchange": "BingX"},
    {"type": "vst", "id": "bingx-x02", "label": "VST demo", "unit": "VST", "exchange": "BingX VST"},
]
SLOTS = [
    {"type": "binance", "label": "Binance", "ready": False},
    {"type": "bybit", "label": "Bybit", "ready": False},
    {"type": "okx", "label": "OKX", "ready": False},
]
TYPE_TO_ID = {l["type"]: l["id"] for l in LANES}
ID_TO_LANE = {l["id"]: l for l in LANES}
PRESET_STORE = UserPresetStore(
    os.path.join(DIR, "user-presets.json"),
    write_overlay=lambda cid, ov: write_overlay(cid, ov),
    lane_ids=[l["id"] for l in LANES],
)


def _short_err(msg) -> str:
    s = " ".join(str(msg or "").split())
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"please verify our authentication.*", "", s, flags=re.I)
    low = s.lower()
    if "signature" in low:
        return ""
    if "insufficient" in low:
        return ""
    if "cooling" in low or "position not exist" in low:
        return ""
    if "quantity or stopprice" in low or "parameter quantity" in low:
        return ""
    if "order size must be less" in low or "available amount" in low:
        return ""
    if "stop loss price should" in low or "take profit price should" in low:
        return ""
    return s.strip(" ,.")[:120]


DETAIL_KEYS = (
    "coord",
    "pulse",
    "indications",
    "engine",
    "variants",
    "exits",
    "block",
    "dca",
    "api",
    "coverage",
    "activity",
    "events",
    "byIndication",
    "byStrategy",
    "klinesTf",
    "tests",
    "signals",
    "prices",
    "regime",
    "cycle",
    "scanMs",
    "rssMb",
    "lastError",
    "activityPerMin",
    "leverage",
    "useMaxLeverage",
    "leverageMap",
    "slPct",
    "tpPct",
    "targetNotional",
    "volumeFactor",
    "cts",
    "pfCost",
    "profitFactor",
    "pf",
    "pfNeutral",
    "pfPlus1xCost",
    "pfScale",
)


def parse_val(v: str):
    v = (v or "").strip()
    if not v:
        return v
    if v[0] in "{[":
        try:
            return json.loads(v)
        except Exception:
            pass
    if v in ("true", "false"):
        return v == "true"
    try:
        if "." in v:
            return float(v)
        return int(v)
    except Exception:
        return v


def qs(path: str) -> dict:
    q = parse_qs(urlparse(path).query)
    return {k: (v[0] if v else "") for k, v in q.items()}


def resolve_conn(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or raw in ("overall", "all"):
        return "overall"
    if raw in TYPE_TO_ID:
        return TYPE_TO_ID[raw]
    return raw.replace("connection:", "")


def redis_hgetall(key: str) -> dict:
    try:
        p = subprocess.run(redis_cli_args("HGETALL", key), capture_output=True, text=True, timeout=6)
    except Exception:
        return {}
    lines = (p.stdout or "").splitlines()
    out = {}
    for i in range(0, len(lines) - 1, 2):
        out[lines[i]] = lines[i + 1]
    return out


def redis_hset(key: str, mapping: dict) -> bool:
    args = redis_cli_args("HSET", key)
    for k, v in mapping.items():
        if v is None:
            continue
        args.extend([str(k), str(v)])
    if len(args) <= 5:
        return False
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=6)
        return p.returncode == 0 and (p.stdout or "").strip().isdigit()
    except Exception:
        return False


def mask_key(k: str) -> str:
    k = (k or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "••••"
    return k[:4] + "…" + k[-4:]


def _conn_type_of(cid: str, raw: dict) -> str:
    if cid == "bingx-x02":
        return "vst"
    if cid == "bingx-x01":
        return "mainnet"
    test = str(raw.get("is_testnet") or "").strip().lower()
    if test in ("1", "true", "yes") or "vst" in str(raw.get("base_url") or "").lower():
        return "vst"
    return "mainnet"


def connection_public(cid: str) -> dict:
    if cid == "overall":
        live = connection_public("bingx-x01")
        vst = connection_public("bingx-x02")
        return {
            "ok": True,
            "conn": "overall",
            "connType": "overall",
            "connectionType": "mainnet",
            "connectionMethod": live.get("connectionMethod") or "library",
            "exchange": "BingX",
            "baseUrl": live.get("baseUrl") or "https://open-api.bingx.com",
            "isTestnet": False,
            "liveTradeEnabled": True,
            "apiKeyMasked": live.get("apiKeyMasked") or "",
            "apiKeySet": bool(live.get("apiKeySet")),
            "apiSecretSet": bool(live.get("apiSecretSet")),
            "lastTestStatus": live.get("lastTestStatus") or "",
            "defaultMainnet": True,
            "lanes": [live, vst],
        }
    raw = redis_hgetall(f"connection:{cid}")
    ctype = _conn_type_of(cid, raw)
    method = (raw.get("connection_method") or "library").strip() or "library"
    default_url = "https://open-api-vst.bingx.com" if ctype == "vst" else "https://open-api.bingx.com"
    live_en = str(raw.get("live_trade_enabled") or "").strip().lower()
    return {
        "ok": True,
        "conn": cid,
        "connType": "vst" if "x02" in cid else "live",
        "connectionType": ctype,
        "connectionMethod": method,
        "exchange": "BingX",
        "baseUrl": (raw.get("base_url") or default_url).rstrip("/"),
        "isTestnet": ctype == "vst",
        "liveTradeEnabled": live_en in ("1", "true", "yes") or ctype == "mainnet",
        "apiKeyMasked": mask_key(raw.get("api_key") or ""),
        "apiKeySet": bool((raw.get("api_key") or "").strip()),
        "apiSecretSet": bool((raw.get("api_secret") or "").strip()),
        "lastTestStatus": raw.get("last_test_status") or "",
        "defaultMainnet": cid == "bingx-x01",
    }


def save_connection(cid: str, body: dict) -> tuple:
    body = body if isinstance(body, dict) else {}
    as_default = body.get("as_default_mainnet")
    if as_default is None:
        as_default = body.get("asDefaultMainnet")
    if as_default is None:
        as_default = cid in ("overall", "bingx-x01", "")
    as_default = bool(as_default)
    ctype = str(body.get("connection_type") or body.get("connectionType") or "").strip().lower()
    method = str(body.get("connection_method") or body.get("connectionMethod") or "library").strip() or "library"
    if method not in ("library", "rest", "hmac"):
        method = "library"
    key = str(body.get("api_key") or body.get("apiKey") or "").strip()
    secret = str(body.get("api_secret") or body.get("apiSecret") or "").strip()
    if as_default or ctype == "mainnet" or cid in ("overall", "bingx-x01", ""):
        target = "bingx-x01"
        write_type = "mainnet"
    else:
        target = cid if cid in TYPE_TO_ID.values() else ("bingx-x02" if "x02" in (cid or "") or ctype == "vst" else "bingx-x01")
        write_type = "vst" if target == "bingx-x02" else "mainnet"
        if ctype in ("mainnet", "vst"):
            write_type = ctype
            target = "bingx-x02" if write_type == "vst" else "bingx-x01"
    cur = redis_hgetall(f"connection:{target}")
    if not key:
        key = (cur.get("api_key") or "").strip()
    if not secret:
        secret = (cur.get("api_secret") or "").strip()
    if not key or not secret:
        return False, "api_key and api_secret required", connection_public(target)
    if write_type == "vst":
        mapping = {
            "api_key": key,
            "api_secret": secret,
            "is_testnet": "1",
            "base_url": "https://open-api-vst.bingx.com",
            "live_trade_enabled": "0",
            "connection_method": method,
            "connection_type": "vst",
            "last_test_status": "saved",
            "updated_at": str(int(time.time())),
        }
    else:
        mapping = {
            "api_key": key,
            "api_secret": secret,
            "is_testnet": "0",
            "base_url": "https://open-api.bingx.com",
            "live_trade_enabled": "1",
            "connection_method": method,
            "connection_type": "mainnet",
            "last_test_status": "saved",
            "updated_at": str(int(time.time())),
        }
    if not redis_hset(f"connection:{target}", mapping):
        return False, "redis write failed", connection_public(target)
    pub = connection_public(target)
    pub["detail"] = f"saved {target} as {write_type} default" if write_type == "mainnet" else f"saved {target}"
    return True, pub["detail"], pub


def overlay_path(conn: str) -> str:
    p = os.path.join(DIR, f"overlay-{conn}.json")
    if os.path.exists(p):
        return p
    return os.path.join(DIR, "overlay.json")


def write_overlay(conn: str, overlay: dict) -> dict:
    with CONTROL_LOCK:
        return _write_overlay_locked(conn, overlay)


def _write_overlay_locked(conn: str, overlay: dict) -> dict:
    cid = resolve_conn(conn) if conn not in ("", "overall") else conn
    if cid in ("", "overall"):
        raise ValueError("pick a lane")
    dest = os.path.join(DIR, f"overlay-{cid}.json")
    cur = load_overlay(cid)
    if not isinstance(overlay, dict):
        overlay = {}
    cur.update(overlay)
    atomic_write(dest, cur)
    return cur


def cts_path(conn: str) -> str:
    return os.path.join(DIR, f"cts-settings-{conn}.json")


def stats_path(conn: str) -> str:
    return os.path.join(DIR, f"stats-{conn}.json")


def slim_for_ui(st: dict) -> dict:
    """Keep switch/UI payloads small: open book + progress, not 500-tile dumps."""
    out = dict(st or {})
    opens = out.get("open") or []
    open_syms = [p.get("symbol") for p in opens if p.get("symbol")]
    px = out.get("prices") or {}
    if isinstance(px, dict):
        out["prices"] = {s: px[s] for s in open_syms if s in px}
    syms = out.get("symbols") or []
    if isinstance(syms, list):
        out["symbolCount"] = out.get("symbolCount") or len(syms)
        keep = list(dict.fromkeys([*open_syms, *syms]))[:64]
        out["symbols"] = keep
    ind = dict(out.get("indications") or {})
    if ind:
        prim = ind.get("primary") or []
        if isinstance(prim, list) and len(prim) > 12:
            ind = dict(ind)
            ind["primary"] = prim[:12]
            out["indications"] = ind
    block = dict(out.get("block") or {})
    if block:
        lanes = block.get("lanes") or []
        if isinstance(lanes, list) and len(lanes) > 8:
            block = dict(block)
            block["laneCount"] = len(lanes)
            block["lanes"] = lanes[:8]
            out["block"] = block
    if isinstance(out.get("signals"), list):
        out["signals"] = out["signals"][:8]
    closed = out.get("closed") or []
    if isinstance(closed, list) and len(closed) > 40:
        out["closed"] = closed[:40]
    return out


def stamp_stats(st: dict, conn: str) -> dict:
    lane = ID_TO_LANE.get(conn) or {}
    out = slim_for_ui(st or {})
    out["connection"] = conn
    out["connType"] = lane.get("type") or out.get("connType") or ("vst" if "x02" in conn else "live")
    out["unit"] = lane.get("unit") or out.get("unit")
    out["exchange"] = lane.get("exchange") or out.get("exchange")
    paused = bool(out.get("paused")) or os.path.exists(os.path.join(DIR, f"PAUSE-{conn}"))
    out["paused"] = paused
    if paused:
        out["halted"] = True
        out["running"] = False
        out["haltReason"] = out.get("haltReason") or "paused"
    # Ground truth: STOP file + systemd state beat a stale stats file, so a
    # stopped/crashed desk never keeps showing its last "running" snapshot.
    stopped = os.path.exists(os.path.join(DIR, f"STOP-{conn}")) or os.path.exists(STOP_ALL_PATH)
    state = unit_state(conn)
    out["svcActive"] = state == "active"
    out["statsAgeS"] = round(stats_age(conn), 1)
    if stopped:
        out["halted"] = True
        out["running"] = False
        out["haltReason"] = "stopped"
    if state != "active":
        out["running"] = False
        out["alive"] = False
        out["stale"] = True
        if not out.get("halted"):
            out["halted"] = True
            out["haltReason"] = "service failed" if state == "failed" else "service inactive"
    elif out["statsAgeS"] > 20:
        out["stale"] = True
    return out


def _touch(path: str) -> None:
    with open(path, "a"):
        pass


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _sysctl(*args: str, timeout: float = 25.0) -> tuple:
    try:
        p = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return 99, str(e)[:160]


_STATE_CACHE: dict = {}

# Single global control mutex: every start/stop/pause/resume — from the desk
# UI, the API, or the heal watchdog — is fully serialized, so rapid clicking
# or a heal tick can never interleave file ops and systemctl calls. Clicks
# queue and apply in arrival order; the last click always wins because it
# executes last.
CONTROL_LOCK = threading.Lock()


def unit_state(cid: str, fresh: bool = False) -> str:
    """systemd is-active state, cached briefly — the desk polls several times a second."""
    now = time.time()
    hit = _STATE_CACHE.get(cid)
    if not fresh and hit and now - hit[0] < 3.0:
        return hit[1]
    rc, out = _sysctl("is-active", pulse_unit(cid), timeout=6)
    state = (out.splitlines() or [""])[0].strip() if out else ""
    if state not in ("active", "inactive", "failed", "activating", "deactivating"):
        state = "failed" if rc not in (0,) and state == "" else (state or "unknown")
    _STATE_CACHE[cid] = (now, state)
    return state


def stats_age(conn: str) -> float:
    try:
        return time.time() - os.path.getmtime(stats_path(conn))
    except Exception:
        return 1e9


def apply_control(conn: str, action: str) -> tuple:
    action = (action or "").lower().strip()
    if action not in ("start", "stop", "pause", "resume"):
        return False, "unknown action"
    if conn not in ("", "overall") and conn not in ID_TO_LANE:
        return False, "unknown conn"
    with CONTROL_LOCK:
        return _apply_control_locked(conn, action)


def _apply_control_locked(conn: str, action: str) -> tuple:
    ids = [l["id"] for l in LANES] if conn in ("", "overall") else [conn]
    notes = []
    for cid in ids:
        pause = os.path.join(DIR, f"PAUSE-{cid}")
        stop = os.path.join(DIR, f"STOP-{cid}")
        run = os.path.join(DIR, f"RUN-{cid}")
        reset_eq = os.path.join(DIR, f"reset-eq-{cid}")
        unit = pulse_unit(cid)
        if action == "pause":
            _unlink(stop)
            _touch(pause)
            notes.append(f"{cid} paused state={unit_state(cid, fresh=True)}")
        elif action in ("start", "resume"):
            _touch(run)
            _unlink(pause)
            _unlink(stop)
            _unlink(STOP_ALL_PATH)
            # Explicit Start = fresh session: engine re-baselines session equity
            # on the next balance tick, so a latched drawdown/equity halt clears.
            _touch(reset_eq)
            rc, out = _sysctl("start", unit)
            if rc != 0:
                # start-limit-hit after a crash loop blocks start — reset and retry once.
                _sysctl("reset-failed", unit, timeout=8)
                rc, out = _sysctl("start", unit)
            st = unit_state(cid, fresh=True)
            notes.append(f"{cid} start rc={rc} state={st}" + ("" if rc == 0 else f" {out[:80]}"))
        elif action == "stop":
            _unlink(run)
            _unlink(pause)
            _touch(stop)
            rc, out = _sysctl("stop", unit)
            st = unit_state(cid, fresh=True)
            notes.append(f"{cid} stop rc={rc} state={st}" + ("" if rc == 0 else f" {out[:80]}"))
    return True, "; ".join(notes)


def load_json(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_cts(conn: str) -> dict:
    path = cts_path(conn)
    if os.path.exists(path):
        data = load_json(path)
        if data:
            return data
    key = f"settings:connection_settings:{conn}"
    raw = redis_hgetall(key)
    out = {name: parse_val(value) for name, value in raw.items()}
    try:
        atomic_write(path, out)
    except Exception:
        pass
    return out


def load_overlay(conn: str) -> dict:
    return load_json(overlay_path(conn))


def load_stats(conn: str) -> dict:
    return load_json(stats_path(conn))


def _sets_lane(lane: dict, st: dict) -> dict:
    sets = st.get("sets") or {}
    prog = sets.get("progress") or {}
    return {
        "type": lane["type"],
        "id": lane["id"],
        "label": lane["label"],
        "progress": prog,
        "activeCount": sets.get("activeCount") or 0,
        "validatedCount": sets.get("validatedCount") or 0,
        "setCount": sets.get("setCount") or 0,
        "ready": bool(sets.get("ready") or prog.get("ready")),
        "histFills": sets.get("histFills") or 0,
        "running": bool(st.get("running")),
        "halted": bool(st.get("halted")),
    }


def lane_summary(lane: dict) -> dict:
    st = load_stats(lane["id"])
    gp = sum(c.get("pnl") or 0 for c in (st.get("closed") or []) if (c.get("pnl") or 0) > 0)
    gl = abs(sum(c.get("pnl") or 0 for c in (st.get("closed") or []) if (c.get("pnl") or 0) < 0))
    pf = (gp / gl) if gl > 0 else (99 if gp > 0 else 0)
    sets = st.get("sets") or {}
    prog = sets.get("progress") or {}
    eng = st.get("engine") or {}
    cov = (st.get("coverage") or {}).get("controls") or {}
    pc = st.get("pfCost") or {}
    stopped = os.path.exists(os.path.join(DIR, f"STOP-{lane['id']}")) or os.path.exists(STOP_ALL_PATH)
    state = unit_state(lane["id"])
    running = bool(st.get("running")) and state == "active" and not stopped
    halted = bool(st.get("halted")) or stopped or state != "active"
    halt_reason = st.get("haltReason")
    if stopped:
        halt_reason = "stopped"
    elif state != "active" and not halt_reason:
        halt_reason = "service failed" if state == "failed" else "service inactive"
    return {
        "type": lane["type"],
        "id": lane["id"],
        "label": lane["label"],
        "unit": lane["unit"],
        "exchange": st.get("exchange") or lane["exchange"],
        "mode": st.get("mode"),
        "running": running,
        "halted": halted,
        "haltReason": halt_reason,
        "svcActive": state == "active",
        "statsAgeS": round(stats_age(lane["id"]), 1),
        "equity": st.get("equity") or 0,
        "available": st.get("available") or 0,
        "unrealized": st.get("unrealized") or 0,
        "openCount": st.get("openCount") or 0,
        "exchangeOpenCount": st.get("exchangeOpenCount", -1),
        "simOpenCount": st.get("simOpenCount", -1),
        "simUPnl": st.get("simUPnl", 0),
        "wins": st.get("wins") or 0,
        "losses": st.get("losses") or 0,
        "sessionPnl": st.get("sessionPnl") or 0,
        "systemPnl": st.get("systemPnl") or st.get("sessionPnl") or 0,
        "systemGrow": st.get("systemGrow") or 0,
        "systemLoss": st.get("systemLoss") or 0,
        "pf": round(pf, 3),
        "scanMs": st.get("scanMs"),
        "rssMb": st.get("rssMb"),
        "errors": st.get("errors") or 0,
        "alive": bool(st) and state == "active",
        "paused": bool(st.get("paused")) or os.path.exists(os.path.join(DIR, f"PAUSE-{lane['id']}")),
        "progressPct": prog.get("pct"),
        "progressPhase": prog.get("phase"),
        "progressDetail": prog.get("detail"),
        "progressReady": bool(prog.get("ready")),
        "progressSymbol": prog.get("symbol") or "",
        "progressSetId": prog.get("setId") or "",
        "progressSymbolsDone": prog.get("symbolsDone"),
        "progressSymbolsTotal": prog.get("symbolsTotal"),
        "progressSetsDone": prog.get("setsDone"),
        "progressSetsTotal": prog.get("setsTotal"),
        "progressBarsDone": prog.get("barsDone"),
        "progressBarsTotal": prog.get("barsTotal"),
        "progressElapsedMs": prog.get("elapsedMs"),
        "progressLastRunMs": prog.get("lastRunMs"),
        "progressCycle": prog.get("cycle"),
        "validatedSetCount": sets.get("validatedCount") or 0,
        "setCount": sets.get("setCount") or 0,
        "progressError": prog.get("error") or "",
        "klinesReady": st.get("klinesReady"),
        "hotMs": eng.get("hotMs") if eng.get("hotMs") is not None else st.get("scanMs"),
        "pfCost": pc.get("ratio"),
        "controlsOk": cov.get("ok") or 0,
        "controlsMissing": cov.get("missing") or 0,
        "controlsSecurity": cov.get("security") or 0,
        "symbolCount": st.get("symbolCount") or len(st.get("symbols") or []),
        "lastError": _short_err(st.get("lastError")),
        "trackPrefix": eng.get("trackPrefix"),
        "cycle": st.get("cycle"),
    }


def merge_activity_summaries(summaries: list) -> dict:
    """Aggregate committed event ledgers without re-counting event tails."""
    scalar_keys = (
        "eventCount", "grossPnl", "fees", "duplicateCount", "requestCount", "responseCount",
        "fillCount", "openEventCount", "closeEventCount", "protectionEventCount", "cancellationCount",
        "errorCount", "internalClosed", "pendingCount", "recoveredCount", "discrepantCount",
    )
    out = {key: 0 for key in scalar_keys}
    out.update({"internalOpen": 0, "exchangeOpen": 0, "byType": {}, "byStatus": {}, "responseCodes": {}, "byIndication": {}, "byStrategy": {}, "byAxis": {}, "tail": [], "source": "committed-event-ledger"})
    exchange_known = True
    parity_bad = False

    def add_map(target: dict, source: object) -> None:
        if not isinstance(source, dict):
            return
        for key, value in source.items():
            if isinstance(value, dict):
                bucket = target.setdefault(str(key), {})
                for name, amount in value.items():
                    try:
                        bucket[str(name)] = int(bucket.get(str(name), 0) or 0) + int(amount or 0)
                    except Exception:
                        continue
            else:
                try:
                    target[str(key)] = int(target.get(str(key), 0) or 0) + int(value or 0)
                except Exception:
                    continue

    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        for key in scalar_keys:
            try:
                out[key] += float(summary.get(key) or 0) if key in ("grossPnl", "fees") else int(summary.get(key) or 0)
            except Exception:
                continue
        try:
            out["internalOpen"] += int(summary.get("internalOpen") or 0)
        except Exception:
            pass
        try:
            exchange = int(summary.get("exchangeOpen", -1))
        except Exception:
            exchange = -1
        if exchange < 0:
            exchange_known = False
        else:
            out["exchangeOpen"] += exchange
        add_map(out["byType"], summary.get("byType"))
        add_map(out["byStatus"], summary.get("byStatus"))
        add_map(out["responseCodes"], summary.get("responseCodes"))
        add_map(out["byIndication"], summary.get("byIndication"))
        add_map(out["byStrategy"], summary.get("byStrategy"))
        add_map(out["byAxis"], summary.get("byAxis"))
        if summary.get("parity") == "discrepant":
            parity_bad = True
        tail = summary.get("tail")
        if isinstance(tail, list):
            out["tail"].extend(row for row in tail if isinstance(row, dict))
    out["eventCount"] = int(out["eventCount"])
    out["duplicateCount"] = int(out["duplicateCount"])
    out["internalOpen"] = int(out["internalOpen"])
    out["exchangeOpen"] = int(out["exchangeOpen"]) if exchange_known else -1
    out["grossPnl"] = round(float(out["grossPnl"]), 8)
    out["fees"] = round(float(out["fees"]), 8)
    out["tail"] = sorted(out["tail"], key=lambda row: float(row.get("ts") or 0), reverse=True)[:32]
    if parity_bad:
        out["parity"] = "discrepant"
    elif not exchange_known:
        out["parity"] = "pending"
    else:
        out["parity"] = "match" if out["internalOpen"] == out["exchangeOpen"] else "discrepant"
    return out


def _pick_detail(lane_defs: list) -> tuple:
    loaded = [(lane, load_stats(lane["id"])) for lane in lane_defs]
    for lane, st in loaded:
        if st and st.get("running") and not st.get("halted"):
            return lane, st
    for lane, st in loaded:
        if st:
            return lane, st
    return lane_defs[0], {}


def merge_overall() -> dict:
    lanes = [lane_summary(l) for l in LANES]
    opens = []
    closed = []
    tests = []
    wins = losses = errors = 0
    running_any = False
    stats_by_id = {}
    activity_summaries = []
    for lane in LANES:
        st = load_stats(lane["id"])
        stats_by_id[lane["id"]] = st
        if not st:
            continue
        if isinstance(st.get("activity"), dict):
            activity_summaries.append(st["activity"])
        elif isinstance((st.get("coverage") or {}).get("activity"), dict):
            activity_summaries.append((st.get("coverage") or {})["activity"])
        running_any = running_any or bool(st.get("running") and not st.get("halted"))
        wins += int(st.get("wins") or 0)
        losses += int(st.get("losses") or 0)
        errors += int(st.get("errors") or 0)
        for p in st.get("open") or []:
            q = dict(p)
            q["connection"] = lane["id"]
            q["connType"] = lane["type"]
            q["unit"] = lane["unit"]
            opens.append(q)
        for c in st.get("closed") or []:
            q = dict(c)
            q["connection"] = lane["id"]
            q["connType"] = lane["type"]
            q["unit"] = lane["unit"]
            closed.append(q)
        tests.extend(st.get("tests") or [])
    closed.sort(key=lambda r: r.get("t") or 0, reverse=True)
    closed = closed[:40]
    live = next((x for x in lanes if x["type"] == "live"), {})
    vst = next((x for x in lanes if x["type"] == "vst"), {})
    wr = (wins / (wins + losses) * 100) if (wins + losses) else 0
    pc = last_n_cost_pf(list(reversed(closed)), 15, POSITION_COST_PCT_DEFAULT)
    pc["minPf"] = 1.1
    pc["pass"] = bool(pc["count"] < 8 or pc["ratio"] + 1e-9 >= 1.1)
    detail_lane, detail_st = _pick_detail(LANES)
    sets_lanes = [_sets_lane(l, stats_by_id.get(l["id"]) or {}) for l in LANES]
    activity = merge_activity_summaries(activity_summaries)
    sets = dict(detail_st.get("sets") or {})
    sets["lanes"] = sets_lanes
    out = {
        "running": running_any,
        "mode": "OVERALL",
        "connection": "overall",
        "connType": "overall",
        "unit": "MIXED",
        "exchange": "All",
        "lanes": lanes,
        "slots": SLOTS,
        "equity": live.get("equity") or 0,
        "equityLive": live.get("equity") or 0,
        "equityVst": vst.get("equity") or 0,
        "available": live.get("available") or 0,
        "usedMargin": 0,
        "unrealized": (live.get("unrealized") or 0) + (vst.get("unrealized") or 0),
        "sessionPnl": (live.get("systemPnl") or live.get("sessionPnl") or 0),
        "sessionPnlLive": live.get("systemPnl") or live.get("sessionPnl") or 0,
        "sessionPnlVst": vst.get("systemPnl") or vst.get("sessionPnl") or 0,
        "systemGrowLive": live.get("systemGrow") or 0,
        "systemLossLive": live.get("systemLoss") or 0,
        "systemGrowVst": vst.get("systemGrow") or 0,
        "systemLossVst": vst.get("systemLoss") or 0,
        "pnlPct": 0,
        "drawdownPct": 0,
        "wins": wins,
        "losses": losses,
        "winRate": round(wr, 1),
        "openCount": len(opens),
        "exchangeOpenCount": sum(l.get("exchangeOpenCount") or 0 for l in lanes if (l.get("exchangeOpenCount") or 0) >= 0) if any((l.get("exchangeOpenCount") or 0) >= 0 for l in lanes) else -1,
        "simOpenCount": sum(l.get("simOpenCount") or 0 for l in lanes if (l.get("simOpenCount") or 0) >= 0) if any((l.get("simOpenCount") or 0) >= 0 for l in lanes) else -1,
        "simUPnl": round(sum(float(l.get("simUPnl") or 0) for l in lanes), 4),
        "maxOpen": 0,
        "open": opens,
        "closed": closed[:80],
        "tests": tests[-24:],
        "activity": activity,
        "events": activity.get("tail") or [],
        "errors": errors,
        "halted": not running_any,
        "paused": any(bool(x.get("paused")) for x in lanes),
        "symbols": [],
        "now": __import__("time").time(),
        "pfCost": pc,
        "profitFactor": pc.get("ratio"),
        "pf": pc.get("ratio"),
        "pfNeutral": 1.0,
        "pfPlus1xCost": 1.1,
        "pfScale": "1.00=neutral · 1.10=+1×PositionCost",
        "detailConn": detail_lane.get("id"),
        "detailType": detail_lane.get("type"),
        "sets": sets,
    }
    try:
        from stats_report import merge_kind_stats, merge_strategy_stats
        cost = float((detail_st.get("pfCost") or {}).get("costPct") or POSITION_COST_PCT_DEFAULT)
        ind = detail_st.get("indications") or {}
        cov = detail_st.get("coverage") or {}
        out["byIndication"] = merge_kind_stats(
            closed,
            cost,
            gate=(sets.get("indGate") or cov.get("indicationGate") or {}),
            hits=cov.get("indicationHits") or ind.get("typeHits") or {},
            types=cov.get("indicationTypes") or ind.get("types") or {},
            kind_live=ind.get("kindStats") or {},
        )
        out["byStrategy"] = merge_strategy_stats(
            closed,
            cost,
            coverage=cov,
            block=detail_st.get("block") or {},
            dca=detail_st.get("dca") or {},
            exits=detail_st.get("exits") or {},
            sets_rows=sets.get("rows") or [],
        )
    except Exception:
        pass
    for k in DETAIL_KEYS:
        if k in ("tests", "activity", "events"):
            continue
        if k in ("pfCost", "profitFactor", "pf", "pfNeutral", "pfPlus1xCost", "pfScale"):
            continue
        if detail_st.get(k) is not None:
            out[k] = detail_st.get(k)
    return slim_for_ui(out)


def connections_blob() -> dict:
    lanes = [lane_summary(l) for l in LANES]
    return {
        "selectedDefault": "overall",
        "types": [
            {
                "type": "overall",
                "label": "Overall",
                "blurb": "All desks in parallel",
                "running": any(l["running"] and not l["halted"] for l in lanes),
                "openCount": sum(l["openCount"] for l in lanes),
                "exchangeOpenCount": sum(l.get("exchangeOpenCount") or 0 for l in lanes if (l.get("exchangeOpenCount") or 0) >= 0) if any((l.get("exchangeOpenCount") or 0) >= 0 for l in lanes) else -1,
                "simOpenCount": sum(l.get("simOpenCount") or 0 for l in lanes if (l.get("simOpenCount") or 0) >= 0) if any((l.get("simOpenCount") or 0) >= 0 for l in lanes) else -1,
                "halted": all(l["halted"] or not l["running"] for l in lanes),
                "progressReady": all(bool(l.get("progressReady")) for l in lanes) if lanes else False,
            },
            *[
                {
                    "type": l["type"],
                    "label": l["label"],
                    "id": l["id"],
                    "unit": l["unit"],
                    "blurb": l["exchange"],
                    "running": l["running"] and not l["halted"],
                    "halted": l["halted"],
                    "paused": l.get("paused"),
                    "equity": l["equity"],
                    "openCount": l["openCount"],
                    "exchangeOpenCount": l.get("exchangeOpenCount", -1),
                    "simOpenCount": l.get("simOpenCount", -1),
                    "simUPnl": l.get("simUPnl", 0),
                    "alive": l["alive"],
                    "progressPct": l.get("progressPct"),
                    "progressPhase": l.get("progressPhase"),
                    "progressReady": l.get("progressReady"),
                    "hotMs": l.get("hotMs"),
                    "pfCost": l.get("pfCost"),
                    "controlsOk": l.get("controlsOk"),
                    "controlsMissing": l.get("controlsMissing"),
                    "symbolCount": l.get("symbolCount"),
                    "haltReason": l.get("haltReason"),
                }
                for l in lanes
            ],
        ],
        "slots": SLOTS,
        "lanes": lanes,
    }


class BoundedHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        request.settimeout(20)
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 1\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIR, **k)

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "GET /stats.json" in msg or "GET /universe.json" in msg or "GET /connections.json" in msg or "GET /connection.json" in msg:
            return
        try:
            append_log(log_path("http.log"), "%s - %s" % (self.address_string(), msg))
        except Exception:
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, code=200):
        blob = json.dumps(obj, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(blob)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        conn = resolve_conn(qs(self.path).get("conn", ""))
        if path in ("/connections.json", "/connections"):
            self._json(connections_blob())
            return
        if path in ("/connection.json", "/connection"):
            self._json(connection_public(conn))
            return
        if path in ("/results-export.json", "/results-export", "/results-export.md"):
            ext = ".md" if path.endswith(".md") else ".json"
            if conn == "overall":
                live = load_stats("bingx-x01")
                vst = load_stats("bingx-x02")
                blob = {
                    "conn": "overall",
                    "connType": "overall",
                    "live": {
                        "connection": "bingx-x01",
                        "openCount": live.get("openCount") or 0,
                        "exchangeOpenCount": live.get("exchangeOpenCount", -1),
                        "simOpenCount": live.get("simOpenCount", -1),
                        "equity": live.get("equity"),
                        "pfCost": live.get("pfCost"),
                        "open": live.get("open") or [],
                        "closed": live.get("closed") or [],
                    },
                    "vst": {
                        "connection": "bingx-x02",
                        "openCount": vst.get("openCount") or 0,
                        "exchangeOpenCount": vst.get("exchangeOpenCount", -1),
                        "simOpenCount": vst.get("simOpenCount", -1),
                        "equity": vst.get("equity"),
                        "pfCost": vst.get("pfCost"),
                        "open": vst.get("open") or [],
                        "closed": vst.get("closed") or [],
                    },
                }
                raw = json.dumps(blob, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", 'attachment; filename="pulse-results-overall.json"')
                self.send_header("Content-Length", str(len(raw)))
                self._cors()
                self.end_headers()
                self.wfile.write(raw)
                return
            cid = conn
            p = os.path.join(DIR, f"results-export-{cid}{ext}")
            if not os.path.exists(p):
                st = load_stats(cid)
                if not st:
                    self._json({"ok": False, "detail": "no export yet"}, 404)
                    return
                raw = json.dumps({
                    "conn": cid,
                    "connType": "vst" if "x02" in cid else "live",
                    "openCount": st.get("openCount") or 0,
                    "exchangeOpenCount": st.get("exchangeOpenCount", -1),
                    "simOpenCount": st.get("simOpenCount", -1),
                    "equity": st.get("equity"),
                    "pfCost": st.get("pfCost"),
                    "open": st.get("open") or [],
                    "closed": st.get("closed") or [],
                    "sets": st.get("sets"),
                    "block": st.get("block"),
                    "coverage": st.get("coverage"),
                }, separators=(",", ":")).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f'attachment; filename="pulse-results-{cid}.json"')
                self.send_header("Content-Length", str(len(raw)))
                self._cors()
                self.end_headers()
                self.wfile.write(raw)
                return
            raw = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown" if ext == ".md" else "application/json")
            self.send_header("Content-Disposition", f'attachment; filename="pulse-results-{cid}{ext}"')
            self.send_header("Content-Length", str(len(raw)))
            self._cors()
            self.end_headers()
            self.wfile.write(raw)
            return
        if path in ("/user-presets.json", "/user-presets"):
            try:
                rows = PRESET_STORE.list()
            except Exception:
                rows = []
            self._json({"ok": True, "presets": rows, "system": True, "max": 24})
            return
        if path in ("/hist-calc.json", "/hist-calc"):
            try:
                from hist_calc import read_job, public_presets
                blob = read_job()
                if not blob.get("presets"):
                    blob["presets"] = public_presets()
                blob["ok"] = True
                blob["independent"] = True
                self._json(blob)
            except Exception as exc:
                self._json({"ok": False, "phase": "error", "detail": str(exc)[:200], "independent": True}, 200)
            return
        if path in ("/config.json", "/config"):
            if conn == "overall":
                self._json({
                    "cts": None,
                    "overlay": None,
                    "conn": "overall",
                    "lanes": [
                        {"type": l["type"], "id": l["id"], "cts": load_cts(l["id"]), "overlay": load_overlay(l["id"])}
                        for l in LANES
                    ],
                })
                return
            self._json({"cts": load_cts(conn), "overlay": load_overlay(conn), "conn": conn})
            return
        if path in ("/stats.json", "/live-stats.json"):
            if conn == "overall":
                self._json(merge_overall())
                return
            st = load_stats(conn)
            if not st:
                self._json(stamp_stats({"running": False, "mode": "OFFLINE", "open": [], "closed": [], "halted": True}, conn))
                return
            self._json(stamp_stats(st, conn))
            return
        if path == "/universe.json":
            self._json(load_json(path_for("universe.json")))
            return
        # Never serve the durable data directory as an arbitrary static tree.
        self._json({"ok": False, "detail": "unknown endpoint"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        conn = resolve_conn(qs(self.path).get("conn", ""))
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json({"ok": False, "detail": "invalid content length"}, 400)
            return
        if n < 0 or n > 1048576:
            self._json({"ok": False, "detail": "request too large"}, 413)
            return
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
            if not isinstance(body, dict):
                raise ValueError("JSON object required")
        except Exception:
            self.send_error(400, "invalid json")
            return
        if path in ("/control.json", "/control"):
            action = str((body or {}).get("action") or "").lower().strip()
            ok, detail = apply_control(conn or "overall", action)
            self._json({"ok": ok, "detail": detail, "conn": conn or "overall", "action": action}, 200 if ok else 400)
            return
        if path in ("/connection.json", "/connection"):
            ok, detail, pub = save_connection(conn or "overall", body if isinstance(body, dict) else {})
            blob = dict(pub or {})
            blob["ok"] = ok
            blob["detail"] = detail
            self._json(blob, 200 if ok else 400)
            return
        if path in ("/user-presets.json", "/user-presets"):
            body = body if isinstance(body, dict) else {}
            action = str(body.get("action") or "save").lower().strip()
            try:
                if action in ("save", "create", "update"):
                    row = PRESET_STORE.save(
                        overlay=body.get("overlay") if isinstance(body.get("overlay"), dict) else {},
                        name=str(body.get("name") or ""),
                        calc_opt=body.get("calcOpt") if isinstance(body.get("calcOpt"), dict) else {},
                        preset_id=str(body.get("id") or "") or None,
                    )
                    self._json({"ok": True, "preset": row, "presets": PRESET_STORE.list(), "detail": f"saved {row.get('name')}"})
                    return
                if action in ("save_default", "default"):
                    row = PRESET_STORE.save_default(
                        overlay=body.get("overlay") if isinstance(body.get("overlay"), dict) else {},
                        calc_opt=body.get("calcOpt") if isinstance(body.get("calcOpt"), dict) else {},
                    )
                    self._json({"ok": True, "preset": row, "presets": PRESET_STORE.list(), "detail": "saved Default"})
                    return
                if action == "rename":
                    row = PRESET_STORE.rename(str(body.get("id") or ""), str(body.get("name") or ""))
                    if not row:
                        self._json({"ok": False, "detail": "preset not found"}, 404)
                        return
                    self._json({"ok": True, "preset": row, "presets": PRESET_STORE.list(), "detail": f"renamed {row.get('name')}"})
                    return
                if action == "delete":
                    okd = PRESET_STORE.delete(str(body.get("id") or ""))
                    detail = "deleted" if okd else "Default is protected or preset not found"
                    self._json({"ok": okd, "presets": PRESET_STORE.list(), "detail": detail}, 200 if okd else 400)
                    return
                if action in ("delete_except_default", "cleanup"):
                    removed = PRESET_STORE.delete_all_except_default()
                    self._json({"ok": True, "removed": removed, "presets": PRESET_STORE.list(), "detail": f"deleted {removed} presets; Default kept"})
                    return
                if action == "load":
                    row, applied = PRESET_STORE.apply(str(body.get("id") or ""), apply_all=body.get("applyAll") is not False)
                    if not row:
                        self._json({"ok": False, "detail": "preset not found"}, 404)
                        return
                    self._json({
                        "ok": True,
                        "preset": row,
                        "overlay": row.get("overlay") or {},
                        "calcOpt": row.get("calcOpt") or {},
                        "applied": applied,
                        "detail": f"loaded {row.get('name')}" + (" · Live + VST" if applied else ""),
                    })
                    return
                self._json({"ok": False, "detail": f"unknown action {action}"}, 400)
            except ValueError as exc:
                self._json({"ok": False, "detail": str(exc)[:160]}, 400)
            except Exception as exc:
                self._json({"ok": False, "detail": str(exc)[:200]}, 200)
            return
        if path in ("/hist-calc.json", "/hist-calc"):
            try:
                from hist_calc import start_job, is_running
                job = start_job(body if isinstance(body, dict) else {})
                job["ok"] = True
                job["running"] = is_running()
                self._json(job)
            except Exception as exc:
                self._json({"ok": False, "phase": "error", "detail": str(exc)[:200]}, 200)
            return
        if path not in ("/config.json", "/config"):
            self.send_error(404)
            return
        if conn == "overall" or not conn:
            self._json({"ok": False, "detail": "pick Live or VST to save overlay"}, 400)
            return
        overlay = body.get("overlay") if isinstance(body, dict) else None
        if not isinstance(overlay, dict):
            overlay = body if isinstance(body, dict) else {}
        try:
            cur = write_overlay(conn, overlay)
        except Exception as exc:
            self._json({"ok": False, "detail": str(exc)[:160]}, 400)
            return
        self._json({"ok": True, "overlay": cur, "conn": conn})


def heal_loop() -> None:
    """Restart crashed/failed engines unless the user stopped them on purpose.
    After a crash loop systemd start-limit leaves a unit dead; reset-failed +
    start revives it, so the desk always comes back on its own."""
    last: dict = {}
    while True:
        try:
            for lane in LANES:
                cid = lane["id"]
                # Same mutex as apply_control: the STOP-file check and the
                # start are atomic against a manual stop/pause click, so the
                # watchdog can never revive a lane the user just stopped.
                with CONTROL_LOCK:
                    if not os.path.exists(os.path.join(DIR, f"RUN-{cid}")):
                        continue
                    if os.path.exists(os.path.join(DIR, f"STOP-{cid}")) or os.path.exists(STOP_ALL_PATH):
                        continue
                    state = unit_state(cid, fresh=True)
                    if state == "active":
                        continue
                    now = time.time()
                    if now - float(last.get(cid, 0) or 0) < 150.0:
                        continue
                    try:
                        p = subprocess.run(
                            redis_cli_args("HGET", f"connection:{cid}", "api_key"),
                            capture_output=True,
                            text=True,
                            timeout=6,
                        )
                        suffix = re.sub(r"[^A-Za-z0-9]", "_", cid).upper()
                        env_key = str(os.environ.get(f"CTS_{suffix}_API_KEY") or os.environ.get(f"BINGX_{suffix}_API_KEY") or "").strip()
                        if not env_key and (p.stdout or "").strip() in ("", "(nil)"):
                            continue  # no keys — engine would exit instantly
                    except Exception:
                        continue
                    last[cid] = now
                    _sysctl("reset-failed", pulse_unit(cid), timeout=8)
                    rc, out = _sysctl("start", pulse_unit(cid))
                try:
                    append_log(log_path("http.log"), f"heal {cid} rc={rc} {out[:120]}")
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(20.0)


if __name__ == "__main__":
    os.chdir(DIR)
    threading.Thread(target=heal_loop, name="heal", daemon=True).start()
    BoundedHTTPServer((os.environ.get("PULSE_HOST", "127.0.0.1"), PULSE_PORT), Handler).serve_forever()
