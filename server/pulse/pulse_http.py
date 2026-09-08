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
from urllib.parse import parse_qs, urlparse, unquote
from position_cost import POSITION_COST_PCT_DEFAULT, last_n_cost_pf
from user_presets import UserPresetStore
from storage_paths import (
    DATA_DIR,
    MAX_RETAINED_FILE_BYTES,
    append_bounded_line,
    atomic_write,
    path_for,
    storage_info,
)
from runtime_scope import redis_key
from redis_coordination import coordinator as redis_config
from set_overview import merge_overviews
from system_settings import calculation_overlay
from runtime_statistics import StatisticsStore, lane_directory, read_status, redis_health

DIR = str(DATA_DIR)
STOP_ALL_PATH = path_for("STOP")
CTS_G_NAME = re.sub(r"[^A-Za-z0-9._-]", "", os.environ.get("CTS_G_NAME", "cts-g")) or "cts-g"
MAX_REQUEST_BYTES = 256 * 1024
MAX_JSON_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_HTML_RESPONSE_BYTES = 8 * 1024 * 1024
STATS_READ_MAX_BYTES = 24 * 1024 * 1024
_STATS_CACHE: dict = {}
_STATS_CACHE_LOCK = threading.Lock()
_OVERLAY_LOCKS = {cid: threading.RLock() for cid in ("bingx-x01", "bingx-x02")}


def _flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _live_start_allowed(cid: str) -> bool:
    """Live (x01) trading is on by default. Tests/operators can opt out."""
    if cid != "bingx-x01":
        return True
    return not _flag("CTS_DISABLE_LIVE_START")


def _live_heal_allowed(cid: str) -> bool:
    if cid != "bingx-x01":
        return True
    return not _flag("CTS_DISABLE_LIVE_HEAL")


def engine_unit(cid: str) -> str:
    """Return the install-scoped engine unit; never control generic grok-* units."""
    return f"{CTS_G_NAME}-pulse@{cid}"

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
    if "109420" in low or "rate limit" in low or "rate-limit" in low or "too many request" in low or "requests within" in low or "request limit" in low:
        return ""
    if "quantity or stopprice" in low or "parameter quantity" in low:
        return ""
    if "order size must be less" in low or "available amount" in low:
        return ""
    if "stop loss price should" in low or "take profit price should" in low:
        return ""
    return s.strip(" ,.")[:120]


DETAIL_KEYS = (
    "forcedConfigs",
    "configEvidence",
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
        redis_config.configure(load_overlay(key.rsplit(":", 1)[-1]))
        return redis_config.read_hash(key)
    except Exception:
        return {}


def redis_hset(key: str, mapping: dict) -> bool:
    try:
        redis_config.configure(load_overlay(key.rsplit(":", 1)[-1]))
        return redis_config.write_hash(key, mapping)
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
    if cid not in ID_TO_LANE:
        return {"ok": False, "conn": cid, "detail": "unknown connection"}
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
    if cid not in ("", "overall", *ID_TO_LANE):
        return False, "unknown connection", connection_public(cid)
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
    cid = resolve_conn(conn)
    if cid not in ID_TO_LANE:
        return os.path.join(DIR, "__invalid-connection-overlay__.json")
    p = os.path.join(DIR, f"overlay-{cid}.json")
    if os.path.exists(p):
        return p
    return os.path.join(DIR, "overlay.json")


def write_overlay(conn: str, overlay: dict) -> dict:
    cid = resolve_conn(conn) if conn not in ("", "overall") else conn
    if cid not in ID_TO_LANE:
        raise ValueError("pick a known lane")
    if not isinstance(overlay, dict):
        raise ValueError("overlay must be an object")
    # Validate before any persistent mutation. JSON's default NaN/Infinity
    # extension otherwise leaves settings that browsers cannot parse.
    json.dumps(overlay, allow_nan=False)
    dest = os.path.join(DIR, f"overlay-{cid}.json")
    # Concurrent partial saves must serialize the complete read/merge/write,
    # not just rename. A common .tmp also collided between HTTP threads.
    with _OVERLAY_LOCKS[cid]:
        cur = load_overlay(cid)
        cur.update(overlay)
        cur = calculation_overlay(cur)
        atomic_write(dest, cur)
    return cur


def cts_path(conn: str) -> str:
    cid = resolve_conn(conn)
    if cid not in ID_TO_LANE:
        return os.path.join(DIR, "__invalid-connection-settings__.json")
    return os.path.join(DIR, f"cts-settings-{cid}.json")


def stats_path(conn: str) -> str:
    cid = resolve_conn(conn)
    if cid not in ID_TO_LANE:
        return os.path.join(DIR, "__invalid-connection-stats__.json")
    return os.path.join(DIR, f"stats-{cid}.json")


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
    if isinstance(opens, list) and len(opens) > 256:
        out["openCountReported"] = len(opens)
        out["openTruncated"] = True
        out["open"] = opens[:256]
    cov = dict(out.get("coverage") or {})
    if cov:
        coord = dict(cov.get("coord") or {})
        if coord:
            variants = dict(coord.get("variants") or {})
            if variants:
                rows = variants.get("rows")
                parents = variants.get("parents")
                if isinstance(rows, list):
                    variants["rowCount"] = variants.get("rowCount") or len(rows)
                    variants.pop("rows", None)
                if isinstance(parents, list):
                    variants["parentCount"] = variants.get("parentCount") or len(parents)
                    variants.pop("parents", None)
                qch = variants.get("qualifiedChildren")
                if isinstance(qch, list):
                    variants["qualifiedChildCount"] = len(qch)
                    variants.pop("qualifiedChildren", None)
                ids = variants.get("parentSetIds")
                if isinstance(ids, list) and len(ids) > 32:
                    variants["parentSetIdCount"] = len(ids)
                    variants["parentSetIds"] = ids[:32]
                coord["variants"] = variants
            cov["coord"] = coord
        hist = dict(cov.get("history") or {})
        if hist and isinstance(hist.get("rows"), list) and len(hist["rows"]) > 24:
            hist = dict(hist)
            hist["rowCount"] = len(hist["rows"])
            hist["rows"] = hist["rows"][:24]
            cov["history"] = hist
        out["coverage"] = cov
    historic = dict(out.get("historic") or {})
    if isinstance(historic.get("rows"), list) and len(historic["rows"]) > 40:
        historic = dict(historic)
        historic["rowCount"] = len(historic["rows"])
        historic["rows"] = historic["rows"][:40]
        out["historic"] = historic
    sets = dict(out.get("sets") or {})
    if isinstance(sets.get("rows"), list) and len(sets["rows"]) > 40:
        sets = dict(sets)
        sets["rowCount"] = len(sets["rows"])
        sets["rows"] = sets["rows"][:40]
        out["sets"] = sets
    lev = out.get("leverageMap")
    if isinstance(lev, dict) and len(lev) > 40:
        keep_lev_syms = set(open_syms) | set((out.get("symbols") or [])[:25])
        out["leverageMap"] = {s: lev[s] for s in keep_lev_syms if s in lev}
        out["leverageMapCount"] = len(lev)
    lev_m = out.get("leverageMax")
    if isinstance(lev_m, dict) and len(lev_m) > 40:
        keep_syms = set(open_syms) | set((out.get("symbols") or [])[:25])
        out["leverageMax"] = {s: lev_m[s] for s in keep_syms if s in lev_m}
    return out


def stamp_stats(st: dict, conn: str) -> dict:
    lane = ID_TO_LANE.get(conn) or {}
    out = slim_for_ui(st or {})
    out["connection"] = conn
    out["connType"] = lane.get("type") or out.get("connType") or ("vst" if "x02" in conn else "live")
    out["unit"] = lane.get("unit") or out.get("unit")
    out["exchange"] = lane.get("exchange") or out.get("exchange")
    # Same progress schema on every connection. Values stay unique per lane.
    sets = out.get("sets") if isinstance(out.get("sets"), dict) else {}
    nested = dict(sets.get("progress") or {}) if isinstance(sets, dict) else {}
    hist = out.get("historic") if isinstance(out.get("historic"), dict) else {}

    def _pick(*vals):
        for v in vals:
            if v is None or v == "":
                continue
            return v
        return None

    progress = {
        "connection": conn,
        "connType": out["connType"],
        "phase": _pick(out.get("progressPhase"), nested.get("phase"), hist.get("phase"), "idle"),
        "pct": _pick(out.get("progressPct"), nested.get("pct"), hist.get("pct"), 0),
        "detail": _pick(out.get("progressDetail"), nested.get("detail"), hist.get("detail"), ""),
        "ready": bool(_pick(out.get("progressReady"), nested.get("ready"), hist.get("ready"), False)),
        "symbol": _pick(out.get("progressSymbol"), nested.get("symbol"), "") or "",
        "setId": _pick(out.get("progressSetId"), nested.get("setId"), "") or "",
        "symbolsDone": _pick(out.get("progressSymbolsDone"), nested.get("symbolsDone"), 0) or 0,
        "symbolsTotal": _pick(out.get("progressSymbolsTotal"), nested.get("symbolsTotal"), 0) or 0,
        "setsDone": _pick(out.get("progressSetsDone"), nested.get("setsDone"), 0) or 0,
        "setsTotal": _pick(out.get("progressSetsTotal"), nested.get("setsTotal"), 0) or 0,
        "barsDone": _pick(out.get("progressBarsDone"), nested.get("barsDone"), 0) or 0,
        "barsTotal": _pick(out.get("progressBarsTotal"), nested.get("barsTotal"), 0) or 0,
        "elapsedMs": _pick(out.get("progressElapsedMs"), nested.get("elapsedMs"), 0) or 0,
        "lastRunMs": _pick(out.get("progressLastRunMs"), nested.get("lastRunMs"), 0) or 0,
        "cycle": _pick(out.get("progressCycle"), nested.get("cycle"), 0) or 0,
        "error": _pick(out.get("progressError"), nested.get("error"), "") or "",
        "validSymbols": list(nested.get("validSymbols") or []),
        "gappedSymbols": list(nested.get("gappedSymbols") or []),
        "missingSymbols": list(nested.get("missingSymbols") or []),
    }
    out["progress"] = progress
    out["progressPhase"] = progress["phase"]
    out["progressPct"] = progress["pct"]
    out["progressDetail"] = progress["detail"]
    out["progressReady"] = progress["ready"]
    out["progressSymbol"] = progress["symbol"]
    out["progressSetId"] = progress["setId"]
    out["progressSymbolsDone"] = progress["symbolsDone"]
    out["progressSymbolsTotal"] = progress["symbolsTotal"]
    out["progressSetsDone"] = progress["setsDone"]
    out["progressSetsTotal"] = progress["setsTotal"]
    out["progressBarsDone"] = progress["barsDone"]
    out["progressBarsTotal"] = progress["barsTotal"]
    out["progressElapsedMs"] = progress["elapsedMs"]
    out["progressLastRunMs"] = progress["lastRunMs"]
    out["progressCycle"] = progress["cycle"]
    out["progressError"] = progress["error"]
    if out.get("symbolCap") is None:
        out["symbolCap"] = (out.get("engine") or {}).get("symbolCap") if isinstance(out.get("engine"), dict) else None
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
    eng = out.get("engine") if isinstance(out.get("engine"), dict) else {}
    load = out.get("load") if isinstance(out.get("load"), dict) else None
    if not load:
        load = eng.get("load") if isinstance(eng, dict) else None
    cov = out.get("coverage") if isinstance(out.get("coverage"), dict) else {}
    if not load and isinstance(cov, dict):
        load = cov.get("load") if isinstance(cov.get("load"), dict) else None
    if isinstance(load, dict) and load:
        out["load"] = load
        out["loadLevel"] = load.get("level") or out.get("loadLevel")
        if isinstance(cov, dict):
            cov = dict(cov)
            cov["load"] = load
            out["coverage"] = cov
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


def _cancel_hist(cid: str) -> None:
    for name in (f"hist-calc-req-{cid}.json", f"hist-calc-{cid}.pid"):
        _unlink(os.path.join(DIR, name))
    if cid not in ("bingx-x01", "bingx-x02"):
        return
    try:
        from hist_calc import stop_job
        stop_job(cid)
    except Exception:
        pass


def _kill_conn_procs(cid: str) -> int:
    """SIGKILL leftover pulse_trader/hist_calc workers for this connection only."""
    me = os.getpid()
    killed = 0
    proc = "/proc"
    try:
        names = os.listdir(proc)
    except OSError:
        return 0
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or pid == me:
            continue
        try:
            env = open(os.path.join(proc, name, "environ"), "rb").read().split(b"\0")
        except OSError:
            continue
        conn = ""
        for item in env:
            if item.startswith(b"PULSE_CONN="):
                conn = item.split(b"=", 1)[-1].decode("utf-8", "ignore")
                break
        if conn != cid:
            continue
        try:
            cmd = open(os.path.join(proc, name, "cmdline"), "rb").read()
        except OSError:
            continue
        if b"pulse_http" in cmd:
            continue
        if b"pulse_trader" not in cmd and b"hist_calc" not in cmd:
            continue
        try:
            os.kill(pid, 9)
            killed += 1
        except OSError:
            pass
    return killed


def _mark_stats_stopped(cid: str) -> None:
    path = stats_path(cid)
    st = load_json(path)
    st["running"] = False
    st["halted"] = True
    st["paused"] = False
    st["alive"] = False
    st["haltReason"] = "stopped"
    try:
        atomic_write(path, st)
    except Exception:
        pass


def _force_stop_lane(cid: str, unit: str) -> str:
    """STOP file first, then systemd stop, then SIGKILL leftovers so Start is clean.

    Exchange positions stay. Heal cannot revive while STOP exists.
    """
    bits = []
    _cancel_hist(cid)
    rc, out = _sysctl("stop", unit, timeout=12)
    if rc != 0 and out:
        bits.append(f"stop {out[:80]}")
    st = unit_state(cid, fresh=True)
    if st in ("active", "activating", "deactivating", "failed", "unknown"):
        _sysctl("kill", "-s", "SIGKILL", "--kill-who=all", unit, timeout=8)
        leftover = _kill_conn_procs(cid)
        if leftover:
            time.sleep(0.25)
        rc2, out2 = _sysctl("stop", unit, timeout=8)
        st = unit_state(cid, fresh=True)
        bits.append("forced")
        if leftover:
            bits.append(f"killed {leftover}")
        if rc2 != 0 and out2:
            bits.append(out2[:60])
    _sysctl("reset-failed", unit, timeout=8)
    _STATE_CACHE.pop(cid, None)
    _mark_stats_stopped(cid)
    st = unit_state(cid, fresh=True)
    extra = (" " + " ".join(bits)) if bits else ""
    return f"{cid} stop rc={rc} state={st}{extra}"


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
    rc, out = _sysctl("is-active", engine_unit(cid), timeout=6)
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
        reset_eq = os.path.join(DIR, f"reset-eq-{cid}")
        unit = engine_unit(cid)
        if action == "pause":
            _unlink(stop)
            _touch(pause)
            notes.append(f"{cid} paused state={unit_state(cid, fresh=True)}")
        elif action in ("start", "resume"):
            if not _live_start_allowed(cid):
                notes.append(f"{cid} start blocked: CTS_DISABLE_LIVE_START")
                continue
            _unlink(pause)
            _unlink(stop)
            _unlink(STOP_ALL_PATH)
            # Explicit Start = fresh session: engine re-baselines session equity
            # on the next balance tick, so a latched drawdown/equity halt clears.
            _touch(reset_eq)
            _sysctl("enable", unit, timeout=8)
            # start is a no-op when the unit is already active, so an in-memory
            # "stopped" latch would stick. Restart always picks up cleared flags.
            verb = "restart" if action == "start" else "start"
            rc, out = _sysctl(verb, unit)
            if rc != 0:
                # start-limit-hit after a crash loop blocks start — reset and retry once.
                _sysctl("reset-failed", unit, timeout=8)
                _sysctl("enable", unit, timeout=8)
                rc, out = _sysctl(verb, unit)
            st = unit_state(cid, fresh=True)
            notes.append(f"{cid} start rc={rc} state={st}" + ("" if rc == 0 else f" {out[:80]}"))
        elif action == "stop":
            _unlink(pause)
            _touch(stop)
            notes.append(_force_stop_lane(cid, unit))
    executed = any("start blocked" not in note for note in notes)
    return executed, "; ".join(notes)


def load_json(path: str) -> dict:
    return load_json_bounded(path, MAX_RETAINED_FILE_BYTES)


def load_json_bounded(path: str, max_bytes: int) -> dict:
    try:
        if not path or not os.path.exists(path):
            return {}
        size = os.path.getsize(path)
        if size <= 0:
            return {}
        limit = max(int(max_bytes or 0), MAX_RETAINED_FILE_BYTES)
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
        if len(raw) > limit:
            return {}
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_cts(conn: str) -> dict:
    conn = resolve_conn(conn)
    if conn not in ID_TO_LANE:
        return {}
    path = cts_path(conn)
    if os.path.exists(path):
        data = load_json(path)
        if data:
            return data
    key = f"settings:connection_settings:{conn}"
    out = {k: parse_val(v) for k, v in redis_hgetall(key).items()}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, path)
    except Exception:
        pass
    return out


def load_overlay(conn: str) -> dict:
    return load_json(overlay_path(conn))


def load_stats(conn: str) -> dict:
    path = stats_path(conn)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = path
    mtime = st.st_mtime
    size = st.st_size
    with _STATS_CACHE_LOCK:
        hit = _STATS_CACHE.get(key)
        if hit and hit[0] == mtime and hit[1] == size:
            return hit[2]
    data = load_json_bounded(path, STATS_READ_MAX_BYTES)
    if not data:
        return {}
    slim = slim_for_ui(data)
    with _STATS_CACHE_LOCK:
        _STATS_CACHE[key] = (mtime, size, slim)
        if len(_STATS_CACHE) > 8:
            for old in list(_STATS_CACHE)[: len(_STATS_CACHE) - 4]:
                _STATS_CACHE.pop(old, None)
    return slim


def _report_number(value, default=0.0) -> float:
    try:
        number = float(value)
        return number if number == number else default
    except (TypeError, ValueError):
        return default


def overall_report_state(live: dict, vst: dict) -> dict:
    """Build a safe combined input for the canonical stats report renderer."""
    states = (live, vst)
    closed = [
        row
        for state in states
        for row in (state.get("closed") or [])
        if isinstance(row, dict)
    ]
    open_positions = [
        row
        for state in states
        for row in (state.get("open") or [])
        if isinstance(row, dict)
    ]
    set_rows = []
    set_count = active_count = validated_count = hist_fills = 0
    for state in states:
        sets = state.get("sets") or {}
        if not isinstance(sets, dict):
            continue
        set_rows.extend(row for row in (sets.get("rows") or []) if isinstance(row, dict))
        set_count += int(_report_number(sets.get("setCount")))
        active_count += int(_report_number(sets.get("activeCount")))
        validated_count += int(_report_number(sets.get("validatedCount")))
        hist_fills += int(_report_number(sets.get("histFills")))
    symbols = sorted({
        str(symbol)
        for state in states
        for symbol in (state.get("symbols") or [])
        if symbol
    })
    wins = sum(1 for row in closed if _report_number(row.get("pnl")) > 0)
    losses = sum(1 for row in closed if _report_number(row.get("pnl")) < 0)
    position_cost = next(
        (
            _report_number((state.get("pfCost") or {}).get("costPct"), POSITION_COST_PCT_DEFAULT)
            for state in states
            if isinstance(state.get("pfCost"), dict) and state.get("pfCost", {}).get("costPct") is not None
        ),
        POSITION_COST_PCT_DEFAULT,
    )
    coverages = [state.get("coverage") or {} for state in states]
    strategies = {
        key: any(bool((coverage.get("strategies") or {}).get(key)) for coverage in coverages)
        for key in {key for coverage in coverages for key in (coverage.get("strategies") or {})}
    }
    indication_types = {
        key: any(bool((coverage.get("indicationTypes") or {}).get(key)) for coverage in coverages)
        for key in {key for coverage in coverages for key in (coverage.get("indicationTypes") or {})}
    }
    historic_states = [state.get("historic") or {} for state in states]
    selected_symbols = sorted({
        str(symbol)
        for historic in historic_states
        for symbol in (historic.get("selectedSymbols") or [])
        if symbol
    })
    valid_symbols = sorted({
        str(symbol)
        for historic in historic_states
        for symbol in (historic.get("validSymbols") or [])
        if symbol
    })
    gapped_symbols = sorted({
        str(symbol)
        for historic in historic_states
        for symbol in (historic.get("gappedSymbols") or [])
        if symbol
    })
    last_watermark = {}
    for index, historic in enumerate(historic_states):
        for symbol, watermark in (historic.get("lastPublishedWatermark") or historic.get("watermark") or {}).items():
            last_watermark[f"lane{index}:{symbol}"] = watermark
    historic_bars = [(historic.get("coverage") or {}).get("bars") or {} for historic in historic_states]
    historic_requested_bars = sum(int(_report_number(bars.get("requested"))) for bars in historic_bars if isinstance(bars, dict))
    historic_completed_bars = sum(int(_report_number(bars.get("completed"))) for bars in historic_bars if isinstance(bars, dict))
    historic_missing_bars = sum(int(_report_number(bars.get("missing"))) for bars in historic_bars if isinstance(bars, dict))
    has_historic = any(bool(historic) for historic in historic_states)
    return {
        "running": any(bool(state.get("running")) and not bool(state.get("halted")) for state in states),
        "mode": "MULTI_DESK",
        "connection": "overall",
        "unit": "MIXED",
        "equity": sum(_report_number(state.get("equity")) for state in states),
        "startEquity": sum(_report_number(state.get("startEquity")) for state in states),
        "available": sum(_report_number(state.get("available")) for state in states),
        "usedMargin": sum(_report_number(state.get("usedMargin")) for state in states),
        "sessionPnl": sum(_report_number(state.get("sessionPnl")) for state in states),
        "realizedPnl": sum(_report_number(state.get("realizedPnl")) for state in states),
        "unrealized": sum(_report_number(state.get("unrealized")) for state in states),
        "wins": wins,
        "losses": losses,
        "openCount": len(open_positions),
        "open": open_positions,
        "closed": closed,
        "symbols": symbols,
        "pfCost": {"n": 15, "costPct": position_cost, "minPf": 1.1},
        "historic": {
            "phase": "aggregate" if has_historic else "offline",
            "coordinationComplete": has_historic and all(bool(historic.get("coordinationComplete")) for historic in historic_states),
            "selectedSymbols": selected_symbols,
            "validSymbols": valid_symbols,
            "gappedSymbols": gapped_symbols,
            "lastPublishedWatermark": last_watermark,
            "lastCompleteRun": max((_report_number(historic.get("lastCompleteRun")) for historic in historic_states), default=0),
            "nextRunAt": min((value for value in (_report_number(historic.get("nextRunAt")) for historic in historic_states) if value > 0), default=0),
            "coverage": {
                "symbols": {"completed": len(valid_symbols), "valid": len(selected_symbols), "gapped": len(gapped_symbols)},
                "bars": {"requested": historic_requested_bars, "completed": historic_completed_bars, "missing": historic_missing_bars},
            },
        },
        "sets": {
            "rows": set_rows,
            "setCount": set_count,
            "activeCount": active_count,
            "validatedCount": validated_count,
            "histFills": hist_fills,
        },
        "coverage": {
            "symbols": len(symbols),
            "px": sum(int(_report_number(coverage.get("px"))) for coverage in coverages),
            "wsOk": all(coverage.get("wsOk") is not False for coverage in coverages),
            "controlsMissing": sum(int(_report_number(coverage.get("controlsMissing"))) for coverage in coverages),
            "qaPass": sum(int(_report_number(coverage.get("qaPass"))) for coverage in coverages),
            "qaFail": sum(int(_report_number(coverage.get("qaFail"))) for coverage in coverages),
            "strategies": strategies,
            "indicationTypes": indication_types,
            "sets": {"setCount": set_count, "activeCount": active_count, "validatedCount": validated_count, "histFills": hist_fills},
        },
        "coord": {"gate": {"allow": all(bool((state.get("coord") or {}).get("gate", {}).get("allow")) for state in states)}},
    }


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


def _num_max(*vals):
    nums = []
    for v in vals:
        if v is None:
            continue
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    return max(nums) if nums else None


def _lane_progress(st: dict) -> dict:
    sets = st.get("sets") or {}
    prog = dict(sets.get("progress") or {})
    hist = st.get("historic") or {}
    nested_detail = str(prog.get("detail") or hist.get("detail") or "")
    top_detail = str(st.get("progressDetail") or "")
    detail = nested_detail if ("slice " in nested_detail or "continuing " in nested_detail) else (top_detail or nested_detail)
    if st.get("progressPhase"):
        return {
            "pct": _num_max(st.get("progressPct"), prog.get("pct"), hist.get("pct")),
            "phase": st.get("progressPhase") or prog.get("phase") or hist.get("phase"),
            "detail": detail,
            "ready": bool(st.get("progressReady") or prog.get("ready") or hist.get("ready")),
            "symbol": st.get("progressSymbol") or prog.get("symbol") or "",
            "setId": st.get("progressSetId") or prog.get("setId") or "",
            "symbolsDone": _num_max(st.get("progressSymbolsDone"), prog.get("symbolsDone")),
            "symbolsTotal": _num_max(st.get("progressSymbolsTotal"), prog.get("symbolsTotal")),
            "setsDone": _num_max(st.get("progressSetsDone"), prog.get("setsDone")),
            "setsTotal": _num_max(st.get("progressSetsTotal"), prog.get("setsTotal")),
            "barsDone": _num_max(st.get("progressBarsDone"), prog.get("barsDone")),
            "barsTotal": _num_max(st.get("progressBarsTotal"), prog.get("barsTotal")),
            "elapsedMs": _num_max(st.get("progressElapsedMs"), prog.get("elapsedMs")),
            "lastRunMs": _num_max(st.get("progressLastRunMs"), prog.get("lastRunMs")),
            "cycle": _num_max(st.get("progressCycle"), prog.get("cycle")),
            "error": st.get("progressError") or prog.get("error") or "",
        }
    hist_phase = str(hist.get("phase") or "")
    if hist_phase in ("backfill", "fetch", "gap", "initial", "catalog", "replay", "score", "partial") and str(prog.get("phase") or "idle") in ("idle", "ready", ""):
        prog = {
            **prog,
            "phase": hist.get("phase"),
            "pct": hist.get("pct") if hist.get("pct") is not None else prog.get("pct"),
            "detail": hist.get("detail") or prog.get("detail"),
            "ready": hist.get("ready") if hist.get("ready") is not None else prog.get("ready"),
        }
    return prog


def lane_summary(lane: dict) -> dict:
    st = load_stats(lane["id"])
    gp = sum(c.get("pnl") or 0 for c in (st.get("closed") or []) if (c.get("pnl") or 0) > 0)
    gl = abs(sum(c.get("pnl") or 0 for c in (st.get("closed") or []) if (c.get("pnl") or 0) < 0))
    pf = (gp / gl) if gl > 0 else (99 if gp > 0 else 0)
    sets = st.get("sets") or {}
    prog = _lane_progress(st)
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
        "loadLevel": (st.get("load") or {}).get("level") if isinstance(st.get("load"), dict) else (eng.get("load") or {}).get("level") if isinstance(eng.get("load"), dict) else st.get("loadLevel"),
        "load": st.get("load") if isinstance(st.get("load"), dict) else (eng.get("load") if isinstance(eng.get("load"), dict) else {}),
    }


def merge_activity_summaries(summaries: list) -> dict:
    """Aggregate committed event ledgers without re-counting event tails."""
    scalar_keys = (
        "eventCount", "grossPnl", "fees", "duplicateCount", "requestCount", "responseCount",
        "fillCount", "openEventCount", "closeEventCount", "protectionEventCount", "cancellationCount",
        "errorCount", "internalClosed", "pendingCount", "recoveredCount", "discrepantCount",
    )
    out = {key: 0 for key in scalar_keys}
    out.update({"internalOpen": 0, "internalPositionGroups": 0, "exchangeOpen": 0, "byType": {}, "byStatus": {}, "responseCodes": {}, "byIndication": {}, "byStrategy": {}, "byAxis": {}, "tail": [], "source": "committed-event-ledger"})
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
            out["internalPositionGroups"] += int(summary.get("internalPositionGroups", summary.get("internalOpen")) or 0)
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
        out["parity"] = "match" if out["internalPositionGroups"] == out["exchangeOpen"] else "discrepant"
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


def merge_axis_enablement(states) -> dict:
    """Overall visibility follows every lane's flags, never one chosen desk."""
    axes = {}
    for state in states:
        runtime = (state.get("coord") or {}).get("axes")
        if runtime is None:
            runtime = ((state.get("coverage") or {}).get("coord") or {}).get("axes") or {}
        for key, value in runtime.items():
            axes.setdefault(key, {"enabled": False})
            axes[key]["enabled"] = axes[key]["enabled"] or value.get("enabled") is True
    return axes


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
    overview = merge_overviews([(lane["label"], (stats_by_id.get(lane["id"], {}).get("sets") or {}).get("overview")) for lane in LANES])
    if overview is not None:
        sets["overview"] = overview
        for key in ("setCount", "activeCount", "validatedCount"):
            sets[key] = sum(int((stats_by_id.get(lane["id"], {}).get("sets") or {}).get(key) or 0) for lane in LANES)
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
        "coord": {"axes": merge_axis_enablement(stats_by_id.values())},
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
        if k in (
            "coverage", "coord", "pulse", "indications", "engine", "variants",
            "exits", "block", "dca", "api", "byIndication", "byStrategy",
            "klinesTf", "signals", "prices", "regime", "cycle", "scanMs", "rssMb",
            "forcedConfigs", "configEvidence",
        ):
            continue
        if detail_st.get(k) is not None:
            out[k] = detail_st.get(k)
    # Unique per-lane progress; overall does not inherit one desk's hist tape.
    out["progress"] = {
        "connection": "overall",
        "connType": "overall",
        "phase": "lanes",
        "pct": None,
        "detail": "per-connection",
        "ready": all(bool(l.get("progressReady")) for l in lanes) if lanes else False,
        "symbol": "",
        "setId": "",
        "symbolsDone": None,
        "symbolsTotal": None,
        "setsDone": None,
        "setsTotal": None,
        "barsDone": None,
        "barsTotal": None,
        "elapsedMs": None,
        "lastRunMs": None,
        "cycle": None,
        "error": "",
        "lanes": [
            {
                "connection": l.get("id"),
                "connType": l.get("type"),
                "phase": l.get("progressPhase"),
                "pct": l.get("progressPct"),
                "detail": l.get("progressDetail"),
                "ready": l.get("progressReady"),
                "symbolsDone": l.get("progressSymbolsDone"),
                "symbolsTotal": l.get("progressSymbolsTotal"),
                "setsDone": l.get("progressSetsDone"),
                "setsTotal": l.get("progressSetsTotal"),
            }
            for l in lanes
        ],
    }
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
                    "progressDetail": l.get("progressDetail"),
                    "progressSymbolsDone": l.get("progressSymbolsDone"),
                    "progressSymbolsTotal": l.get("progressSymbolsTotal"),
                    "progressSetsDone": l.get("progressSetsDone"),
                    "progressSetsTotal": l.get("progressSetsTotal"),
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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DIR, **k)

    def log_message(self, fmt, *args):
        msg = fmt % args
        if "GET /stats.json" in msg or "GET /universe.json" in msg or "GET /connections.json" in msg or "GET /connection.json" in msg:
            return
        try:
            path = os.path.join(DIR, "http.log")
            append_bounded_line(path, "%s - %s\n" % (self.address_string(), msg))
        except Exception:
            pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")

    def _json(self, obj, code=200):
        blob = json.dumps(obj, separators=(",", ":")).encode()
        if len(blob) > MAX_JSON_RESPONSE_BYTES:
            obj = {"ok": False, "detail": "response exceeds bounded payload limit"}
            code = 500
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
        normalized_path = "/" + os.path.normpath(unquote(path)).lstrip("/")
        if normalized_path == "/statistics" or normalized_path.startswith("/statistics/"):
            self._json({"ok": False, "detail": "statistics files are private; use the status endpoint"}, 404)
            return
        conn = resolve_conn(qs(self.path).get("conn", ""))
        if path == "/system.json":
            if conn == "overall":
                self._json({"connection": "overall", "lanes": [read_status(DIR, lane["id"]) for lane in LANES], "sharedDatabase": redis_health()})
            elif conn in ID_TO_LANE:
                self._json({**read_status(DIR, conn), "sharedDatabase": redis_health()})
            else:
                self._json({"ok": False, "detail": "pick a known connection"}, 400)
            return
        if path in ("/connections.json", "/connections"):
            self._json(connections_blob())
            return
        if path in ("/connection.json", "/connection"):
            self._json(connection_public(conn))
            return
        if path in ("/results-export.json", "/results-export", "/results-export.md", "/results-export.html"):
            ext = ".html" if path.endswith(".html") else ".md" if path.endswith(".md") else ".json"
            if conn != "overall" and conn not in ID_TO_LANE:
                self._json({"ok": False, "detail": "unknown connection"}, 404)
                return

            raw: bytes
            if conn == "overall":
                live = load_stats("bingx-x01")
                vst = load_stats("bingx-x02")
                if ext == ".html" or ext == ".md":
                    from stats_report import build as build_report, render_html, render_md
                    state = overall_report_state(live, vst)
                    cost_pct = _report_number((state.get("pfCost") or {}).get("costPct"), POSITION_COST_PCT_DEFAULT)
                    report = build_report(state, cost_pct=cost_pct, conn="overall")
                    raw = (render_html(report) if ext == ".html" else render_md(report)).encode("utf-8")
                else:
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
            else:
                cid = conn
                p = os.path.join(DIR, f"results-export-{cid}{ext}")
                st = load_stats(cid)
                if os.path.exists(p):
                    with open(p, "rb") as export_file:
                        raw = export_file.read()
                elif not st:
                    self._json({"ok": False, "detail": "no export yet"}, 404)
                    return
                elif ext == ".html" or ext == ".md":
                    from stats_report import build as build_report, render_html, render_md
                    cost_pct = _report_number((st.get("pfCost") or {}).get("costPct"), POSITION_COST_PCT_DEFAULT)
                    report = build_report(st, cost_pct=cost_pct, conn=cid)
                    raw = (render_html(report) if ext == ".html" else render_md(report)).encode("utf-8")
                else:
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

            max_bytes = MAX_HTML_RESPONSE_BYTES if ext == ".html" else MAX_JSON_RESPONSE_BYTES
            if len(raw) > max_bytes:
                self._json({"ok": False, "detail": "export exceeds bounded payload limit"}, 500)
                return
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".md": "text/markdown; charset=utf-8",
                ".json": "application/json",
            }[ext]
            disposition = "inline" if ext == ".html" else "attachment"
            filename = f"pulse-results-{conn}{ext}"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'{disposition}; filename="{filename}"')
            self.send_header("Content-Length", str(len(raw)))
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
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
                from hist_calc import public_presets, read_job
                blob = read_job(conn)
                if not blob.get("presets"):
                    blob["presets"] = public_presets()
                blob["ok"] = True
                blob["connection"] = conn
                blob["shared"] = True
                blob["independent"] = False
                self._json(blob)
            except Exception as exc:
                self._json({"ok": False, "phase": "error", "detail": str(exc)[:200], "connection": conn, "shared": True, "independent": False}, 200)
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
            ov = load_overlay(conn)
            self._json({
                "cts": load_cts(conn),
                "overlay": ov,
                "conn": conn,
                "connType": "vst" if "x02" in conn else "live",
                "symbolCap": ov.get("symbolCap"),
                "symbolsAll": ov.get("symbolsAll"),
                "histLookbackBars": ov.get("histLookbackBars"),
                "maxOpen": ov.get("maxOpen"),
                "normalExecutionEnabled": ov.get("normalExecutionEnabled"),
                "controlOrders": ov.get("controlOrders"),
                "controlOrdersPerConfig": ov.get("controlOrdersPerConfig"),
                "dcaEnabled": ov.get("dcaEnabled"),
                "blockActive": ov.get("blockActive"),
            })
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
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        conn = resolve_conn(qs(self.path).get("conn", ""))
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            self.send_error(400, "invalid content length")
            return
        if n < 0 or n > MAX_REQUEST_BYTES:
            self._json({"ok": False, "detail": "request exceeds bounded payload limit"}, 413)
            return
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            self.send_error(400, "invalid json")
            return
        if path == "/system.json":
            # The dashboard proxy is local. Reject direct cross-origin browser
            # writes to this maintenance endpoint even though old GETs use CORS.
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "").split(":", 1)[0]
            if origin and urlparse(origin).hostname != host:
                self._json({"ok": False, "detail": "same-origin maintenance required"}, 403)
                return
            if conn not in ID_TO_LANE or not isinstance(body, dict):
                self._json({"ok": False, "detail": "select one connection for maintenance"}, 400)
                return
            if not (lane_directory(DIR, conn) / "statistics.sqlite3").exists():
                self._json({"ok": False, "detail": "statistics database has not been initialized"}, 409)
                return
            store = None
            try:
                action = body.get("action")
                if action not in ("backup", "compact", "reset"):
                    raise ValueError("choose backup, compact or reset")
                store = StatisticsStore(DIR, conn, load_overlay(conn))
                if action == "reset":
                    result = store.reset(body.get("scope"), body.get("confirmation"))
                elif action == "backup":
                    result = {"ok": True, "detail": "Verified statistics backup saved", "backup": store.backup()}
                else:
                    store.maintain()
                    result = {"ok": True, "detail": "Expired details pruned and database compacted; totals preserved"}
                self._json(result)
            except ValueError as exc:
                self._json({"ok": False, "detail": str(exc)}, 400)
            except Exception as exc:
                self._json({"ok": False, "detail": f"Maintenance failed: {type(exc).__name__}; retry is safe"}, 503)
            finally:
                if store:
                    store.close()
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
                from hist_calc import start_job
                job = start_job(body if isinstance(body, dict) else {}, connection=conn)
                job["ok"] = True
                job["running"] = job.get("phase") in (
                    "queued", "initial", "hourly", "fetch", "backfill", "gap", "replay", "score", "incremental"
                )
                self._json(job)
            except Exception as exc:
                self._json({"ok": False, "phase": "error", "detail": str(exc)[:200], "connection": conn, "shared": True, "independent": False}, 200)
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
    start revives it, so the desk always comes back on its own.

    A live unit whose stats file stops moving is treated as stuck: first ask
    it to trim caches, then (after a long stall) recycle the unit.
    """
    last: dict = {}
    last_trim: dict = {}
    while True:
        try:
            for lane in LANES:
                cid = lane["id"]
                if not _live_heal_allowed(cid):
                    continue
                # Same mutex as apply_control: the STOP-file check and the
                # start are atomic against a manual stop/pause click, so the
                # watchdog can never revive a lane the user just stopped.
                with CONTROL_LOCK:
                    if os.path.exists(os.path.join(DIR, f"STOP-{cid}")) or os.path.exists(STOP_ALL_PATH):
                        continue
                    if os.path.exists(os.path.join(DIR, f"PAUSE-{cid}")):
                        continue
                    state = unit_state(cid, fresh=True)
                    now = time.time()
                    age = stats_age(cid)
                    if state == "active":
                        if age > 75.0:
                            trim_path = os.path.join(DIR, f"HEAL-TRIM-{cid}")
                            if now - float(last_trim.get(cid, 0) or 0) >= 60.0:
                                last_trim[cid] = now
                                try:
                                    with open(trim_path, "a"):
                                        pass
                                    append_bounded_line(os.path.join(DIR, "http.log"), f"heal-trim {cid} statsAge={age:.0f}s\n")
                                except Exception:
                                    pass
                        if age > 300.0 and now - float(last.get(cid, 0) or 0) >= 180.0:
                            last[cid] = now
                            unit = engine_unit(cid)
                            _sysctl("reset-failed", unit, timeout=8)
                            rc, out = _sysctl("restart", unit)
                            try:
                                append_bounded_line(os.path.join(DIR, "http.log"), f"heal-stuck {cid} age={age:.0f}s rc={rc} {out[:120]}\n")
                            except Exception:
                                pass
                        continue
                    if now - float(last.get(cid, 0) or 0) < 150.0:
                        continue
                    suffix = re.sub(r"[^A-Za-z0-9]", "_", cid).upper()
                    env_key = str(os.environ.get(f"CTS_{suffix}_API_KEY") or os.environ.get(f"BINGX_{suffix}_API_KEY") or "").strip()
                    if not env_key:
                        try:
                            env_key = redis_hgetall(f"connection:{cid}").get("api_key", "").strip()
                        except Exception:
                            env_key = ""
                    if not env_key:
                        continue  # no keys — engine would exit instantly
                    last[cid] = now
                    unit = engine_unit(cid)
                    _sysctl("enable", unit, timeout=8)
                    _sysctl("reset-failed", unit, timeout=8)
                    rc, out = _sysctl("start", unit)
                try:
                    append_bounded_line(os.path.join(DIR, "http.log"), f"heal {cid} rc={rc} {out[:120]}\n")
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(20.0)


if __name__ == "__main__":
    os.chdir(DIR)
    threading.Thread(target=heal_loop, name="heal", daemon=True).start()
    ThreadingHTTPServer((os.environ.get("PULSE_HOST", "127.0.0.1"), int(os.environ.get("PULSE_PORT", "3015"))), Handler).serve_forever()
