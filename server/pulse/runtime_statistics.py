"""Lane-owned bounded statistics. Never opens order, credential or history files.

SQLite contains reporting totals, recent evidence and resource samples. Detail
retention never subtracts from totals. Engine evaluation tapes remain separate.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
import subprocess

from system_settings import normalize_system_settings

MIB = 1024 * 1024
TABLES = ("meta", "trades", "events", "samples")
_REDIS_CACHE = {}
_REDIS_LOCK = threading.Lock()


def redis_health():
    """Metadata only. Never enumerate values, expire keys or alter shared Redis."""
    with _REDIS_LOCK:
        if time.monotonic() - _REDIS_CACHE.get("at", -100) < 30:
            return dict(_REDIS_CACHE["value"])
        result = {"shared": True, "available": False, "sampledAt": time.time()}
        try:
            response = subprocess.run(["redis-cli", "--raw", "INFO", "all"], capture_output=True, text=True, timeout=1)
            if response.returncode != 0:
                raise RuntimeError("redis metadata unavailable")
            fields = dict(line.strip().split(":", 1) for line in response.stdout.splitlines() if ":" in line and not line.startswith("#"))
            if "used_memory" not in fields:
                raise RuntimeError("redis metadata unavailable")
            databases = [dict(part.split("=", 1) for part in value.split(",") if "=" in part)
                         for key, value in fields.items() if re.fullmatch(r"db\d+", key)]
            result.update(available=True, keys=sum(int(row.get("keys", 0)) for row in databases),
                          expiringKeys=sum(int(row.get("expires", 0)) for row in databases),
                          memoryBytes=int(fields.get("used_memory", 0)), maxMemoryBytes=int(fields.get("maxmemory", 0)),
                          operationsPerSec=int(fields.get("instantaneous_ops_per_sec", 0)),
                          policy=fields.get("maxmemory_policy", "unknown"),
                          appendOnly=fields.get("aof_enabled") == "1", snapshotStatus=fields.get("rdb_last_bgsave_status", "unknown"),
                          appendStatus=fields.get("aof_last_write_status", "unknown"))
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            result["detail"] = "Shared Redis metadata unavailable"
        _REDIS_CACHE.update(at=time.monotonic(), value=result)
        return dict(result)


def lane_directory(root, connection):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", str(connection)):
        raise ValueError("invalid statistics connection")
    return Path(root) / "statistics" / connection


def number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def compact(value, limit=16384):
    encoded = json.dumps(value, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > limit:
        raise ValueError("statistics record exceeds size limit")
    return encoded


class StatisticsStore:
    def __init__(self, root, connection, settings=None):
        self.connection = connection
        self.directory = lane_directory(root, connection)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "statistics.sqlite3"
        self.lock = threading.RLock()
        self.writes = 0
        self.settings = normalize_system_settings(settings)
        self.db = sqlite3.connect(self.path, timeout=0.2, check_same_thread=False)
        self.path.chmod(0o600)
        self.db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA cache_size=-1024")
        self.db.execute("PRAGMA wal_autocheckpoint=64")
        self.db.execute("PRAGMA journal_size_limit=262144")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades (id TEXT PRIMARY KEY, t REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS trades_time ON trades(t);
            CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, t REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_time ON events(t);
            CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, t REAL NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS samples_time ON samples(t);
        """)
        self.configure(settings)

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self.db.execute("INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, compact(value)))

    def configure(self, settings):
        self.settings = normalize_system_settings(settings)
        with self.lock:
            size = int(self.db.execute("PRAGMA page_size").fetchone()[0])
            self.db.execute(f"PRAGMA max_page_count={int(self.settings['systemDbMaxMb'] * MIB / size)}")
            with self.db:
                self._set("limits", self.settings)

    def begin_session(self):
        with self.lock, self.db:
            old = self.get("session", {})
            counters = self.get("counters", {})
            unclean = bool(old and not old.get("clean", False))
            counters["sessions"] = int(counters.get("sessions", 0)) + 1
            counters["crashes"] = int(counters.get("crashes", 0)) + int(unclean)
            counters["recoveries"] = int(counters.get("recoveries", 0)) + int(unclean)
            session = {"id": uuid.uuid4().hex, "pid": os.getpid(), "startedAt": time.time(), "clean": False}
            self._set("session", session)
            self._set("counters", counters)
            return session

    def finish_session(self, session_id, clean=True):
        with self.lock, self.db:
            session = self.get("session", {})
            if session.get("id") == session_id:
                session.update(clean=bool(clean), endedAt=time.time())
                self._set("session", session)

    def increment(self, key, amount=1):
        if key not in ("recoveries", "requests", "errors", "totalRunS"):
            raise ValueError("unknown runtime counter")
        with self.lock, self.db:
            counters = self.get("counters", {})
            counters[key] = number(counters.get(key)) + max(0, number(amount))
            self._set("counters", counters)

    def record_trade(self, row, capital=0.0):
        # Only confirmed own exchange deltas affect persistent financial totals.
        if not row.get("exchange_confirmed") or row.get("ours") is False:
            return False
        if row.get("conn") and row["conn"] != self.connection:
            return False
        stamp = number(row.get("t"))
        identity = str(row.get("close_fill_id") or "") or hashlib.sha256(
            compact([row.get(k) for k in ("client_id", "t", "symbol", "side", "qty", "pnl")]).encode()
        ).hexdigest()
        payload = compact(row)
        with self.lock:
            self.writes += 1
            if self.writes % 64 == 0:
                self.maintain()
        with self.lock, self.db:
            floor = max(number(self.get("tradeFloor", 0)), number(self.get("statisticsResetAt", 0)))
            if stamp <= floor:
                return False
            cur = self.db.execute("INSERT OR IGNORE INTO trades VALUES (?,?,?)", (identity, stamp, payload))
            if not cur.rowcount:
                return False
            totals = self.get("totals", {})
            pnl = number(row.get("pnl"))
            for key, value in {
                "n": 1, "wins": int(pnl > 0), "losses": int(pnl < 0),
                "grow": max(0, pnl), "loss": max(0, -pnl), "realized": pnl,
                "fees": max(0, number(row.get("fee_total"))),
                "tradedNotional": abs(number(row.get("qty")) * number(row.get("entry"))),
            }.items():
                totals[key] = number(totals.get(key)) + value
            totals["since"] = min(number(totals.get("since"), stamp), stamp)
            totals["updatedAt"] = max(number(totals.get("updatedAt")), stamp)
            totals["capital"] = number(totals.get("capital")) or max(0, number(capital))
            peak = max(number(totals.get("peakPnl")), totals["realized"])
            totals["peakPnl"] = peak
            dd = peak - totals["realized"]
            totals["maxDrawdown"] = max(number(totals.get("maxDrawdown")), dd)
            base = totals["capital"] + peak
            if totals["capital"] > 0 and base > 0:
                totals["maxDrawdownPct"] = max(number(totals.get("maxDrawdownPct")), dd / base * 100)
            if dd > 1e-12:
                totals["ddSince"] = number(totals.get("ddSince")) or stamp
                totals["maxDdtS"] = max(number(totals.get("maxDdtS")), stamp - totals["ddSince"])
            else:
                if totals.get("ddSince"):
                    totals["maxDdtS"] = max(number(totals.get("maxDdtS")), stamp - totals["ddSince"])
                totals["ddSince"] = 0
            self._set("totals", totals)
            return True

    def record_events(self, rows):
        for offset in range(0, len(rows), 64):
            self.maintain()
            with self.lock, self.db:
                reset_at = number(self.get("telemetryResetAt", 0))
                for row in rows[offset:offset + 64]:
                    stamp = number(row.get("ts"))
                    if stamp <= reset_at:
                        continue
                    # Persist scalar evidence, never an arbitrary exchange response.
                    slim = {k: v for k, v in row.items() if k != "metadata" and isinstance(v, (str, int, float, bool))}
                    self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)",
                                    (str(row.get("event_id", ""))[:160], stamp, compact(slim, 8192)))

    def sample(self, snapshot, elapsed, requests, errors, recovered):
        with self.lock, self.db:
            counters = self.get("counters", {})
            for key, value in {"totalRunS": elapsed, "requests": requests, "errors": errors, "recoveries": recovered}.items():
                counters[key] = number(counters.get(key)) + max(0, number(value))
            self._set("counters", counters)
            self._set("latest", snapshot)
            self.db.execute("INSERT INTO samples(t,payload) VALUES (?,?)", (time.time(), compact(snapshot)))

    def maintain(self, pressure=False):
        """Expiry and row/byte ceilings only prune reporting evidence, never totals."""
        cutoff = time.time() - self.settings["systemRetentionDays"] * 86400
        with self.lock, self.db:
            size = int(self.db.execute("PRAGMA page_size").fetchone()[0])
            used = self.db.execute("PRAGMA page_count").fetchone()[0] - self.db.execute("PRAGMA freelist_count").fetchone()[0]
            pressure = pressure or used * size > self.settings["systemDbMaxMb"] * MIB * 0.75
            for table, key in (("trades", "systemTradeMaxRows"), ("events", "systemEventMaxRows"), ("samples", "systemSampleMaxRows")):
                limit = int(self.settings[key])
                if pressure:
                    count = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    limit = min(limit, max(0, count * 3 // 4))
                # The floor prevents an old retained JSON tail being reimported on restart.
                if table == "trades":
                    old = self.db.execute("SELECT MAX(t) FROM trades WHERE t<? OR id IN (SELECT id FROM trades ORDER BY t DESC,id DESC LIMIT -1 OFFSET ?)", (cutoff, limit)).fetchone()[0]
                    if old is not None:
                        self._set("tradeFloor", max(number(self.get("tradeFloor")), old))
                self.db.execute(f"DELETE FROM {table} WHERE t<? OR id IN (SELECT id FROM {table} ORDER BY t DESC,id DESC LIMIT -1 OFFSET ?)", (cutoff, limit))
        with self.lock:
            self.db.execute("PRAGMA incremental_vacuum(256)")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def backup(self, record=True):
        directory = self.directory / "backups"
        directory.mkdir(exist_ok=True, mode=0o700)
        target = directory / f"statistics-{time.time_ns()}.sqlite3"
        tmp = target.with_suffix(".tmp")
        try:
            # Separate reader lets live commits continue between backup pages.
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=0.2)) as source:
                with closing(sqlite3.connect(tmp)) as destination:
                    source.backup(destination, pages=64, sleep=0.01)
                    if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("statistics backup verification failed")
            tmp.chmod(0o600)
            os.replace(tmp, target)
            for old in sorted(directory.glob("statistics-*.sqlite3"), reverse=True)[int(self.settings["systemBackupKeep"]):]:
                old.unlink()
            if record:
                with self.lock, self.db:
                    self._set("lastBackupAt", time.time())
            return target.name
        finally:
            tmp.unlink(missing_ok=True)

    def reset(self, scope, confirmation):
        if scope not in ("telemetry", "statistics"):
            raise ValueError("choose telemetry or statistics")
        if confirmation != f"RESET {scope.upper()} {self.connection}":
            raise ValueError("exact connection confirmation required")
        with self.lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            backup = self.backup(record=False)
            self._set("lastBackupAt", time.time())
            self.db.execute("DELETE FROM samples")
            self.db.execute("DELETE FROM events")
            self._set("telemetryResetAt", time.time())
            self._set("counters", {})
            self._set("latest", {})
            if scope == "statistics":
                self.db.execute("DELETE FROM trades")
                self._set("totals", {})
                self._set("statisticsResetAt", time.time())
            self._set("lastReset", {"scope": scope, "at": time.time(), "backup": backup})
        self.maintain()
        return {"ok": True, "connection": self.connection, "scope": scope, "backup": backup,
                "detail": f"{scope.capitalize()} reset; verified backup saved. Evaluation history and open orders preserved."}

    def status(self):
        with self.lock:
            counts = {table: self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
            result = {"connection": self.connection, "persistent": True, "dbFile": str(self.path),
                      "directory": str(self.directory), "dbKeys": sum(counts.values()), "dbRows": counts,
                      "dbBytes": sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")) if p.exists()),
                      "counters": self.get("counters", {}), "totals": self.get("totals", {}),
                      "session": self.get("session", {}), "limits": self.get("limits", self.settings),
                      "lastBackupAt": self.get("lastBackupAt", 0), "lastReset": self.get("lastReset", {}),
                      **self.get("latest", {})}
            result["snapshotAt"] = number(result.get("sampledAt"))
            return result

    def close(self):
        with self.lock:
            self.db.close()


def read_status(root, connection):
    """Read-only HTTP status; inspecting an unstarted lane never creates its DB."""
    directory = lane_directory(root, connection)
    path = directory / "statistics.sqlite3"
    if not path.exists():
        return {"connection": connection, "persistent": False, "detail": "Awaiting first engine statistics checkpoint"}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.2)) as db:
            db.execute("PRAGMA query_only=ON")
            meta = {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}
            counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
        size = sum(p.stat().st_size for p in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")) if p.exists())
        latest = meta.get("latest", {})
        return {**latest, "connection": connection, "persistent": True, "dbFile": str(path), "directory": str(directory),
                "dbKeys": sum(counts.values()), "dbRows": counts, "dbBytes": size, "snapshotAt": latest.get("sampledAt", 0),
                **{k: meta.get(k, {}) for k in ("counters", "totals", "session", "limits", "lastReset")},
                "lastBackupAt": meta.get("lastBackupAt", 0)}
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {"connection": connection, "persistent": False, "error": f"Statistics unavailable: {type(exc).__name__}"}


class RuntimeMonitor:
    """Resource sampling on a dedicated bounded worker, never the order loop."""
    def __init__(self, root, connection, settings=None):
        self.store = StatisticsStore(root, connection, settings)
        self.session = self.store.begin_session()
        self.started = self.last_mono = time.monotonic()
        self.last_cpu = time.process_time()
        self.last_requests = self.last_errors = 0
        self.failed = False
        self.recovered = 0
        self.last_error = ""
        self.counter_lock = threading.Lock()
        self.snapshot = self.store.status()
        self.stop = threading.Event()
        self.thread = None

    def configure(self, settings):
        self.store.configure(settings)

    def note_failure(self):
        with self.counter_lock:
            self.failed = True

    def note_success(self):
        with self.counter_lock:
            if self.failed:
                self.recovered += 1
                self.failed = False

    def record_trade(self, row, capital=0):
        try:
            changed = self.store.record_trade(row, capital)
            if changed:
                self.snapshot = {**self.snapshot, "totals": self.store.get("totals", {})}
            return changed
        except (sqlite3.Error, OSError, ValueError) as exc:
            self.last_error = f"Statistics write: {type(exc).__name__}"
            return False  # The bounded engine trade journal is retried on the next sample.

    def sample(self, pulse):
        now = time.monotonic()
        elapsed = max(1e-9, now - self.last_mono)
        cpu = time.process_time()
        requests = int(getattr(pulse.api, "stats", {}).get("rest", 0))
        errors = int(getattr(pulse, "errors", 0))
        delta_requests = max(0, requests - self.last_requests)
        from load_engine import rss_mb
        metrics = {"sampledAt": time.time(), "cpuPct": max(0, (cpu - self.last_cpu) / elapsed * 100),
                   "memoryMb": rss_mb(), "requestsPerSec": delta_requests / elapsed,
                   "sessionRunningS": now - self.started, "cycle": int(getattr(pulse, "cycle", 0)),
                   "internalGeneralEnabled": True,
                   "normalExecutionEnabled": bool(getattr(pulse, "normal_execution_enabled", False))}
        cache = getattr(pulse, "calculation_cache", None)
        if cache:
            metrics["calculationCache"] = cache.status()
        self.store.maintain()
        # Replay recent committed deltas idempotently after a transient storage failure.
        from dataclasses import asdict, is_dataclass
        for row in list(getattr(pulse, "closed", [])):
            self.store.record_trade(asdict(row) if is_dataclass(row) else row, getattr(pulse, "start_eq", 0))
        ledger = getattr(pulse, "event_ledger", None)
        if ledger:
            with ledger._lock:
                events = [e.as_dict() for e in ledger.events]
            self.store.record_events(events)
            ledger.flush()
        with self.counter_lock:
            recovered = self.recovered
        self.store.sample(metrics, elapsed, delta_requests, max(0, errors - self.last_errors), recovered)
        self.last_mono, self.last_cpu = now, cpu
        self.last_requests, self.last_errors = requests, errors
        with self.counter_lock:
            self.recovered -= recovered
        self.store.maintain()
        if time.time() - number(self.store.get("lastBackupAt")) >= self.store.settings["systemBackupIntervalHours"] * 3600:
            self.store.backup()
        self.last_error = ""
        self.snapshot = self.store.status()
        return self.snapshot

    def start(self, pulse):
        def worker():
            while not self.stop.is_set():
                try:
                    self.sample(pulse)
                except Exception as exc:
                    self.last_error = f"Statistics checkpoint: {type(exc).__name__}"
                    self.snapshot = {**self.snapshot, "error": self.last_error}
                self.stop.wait(self.store.settings["systemMetricsIntervalS"])
        self.thread = threading.Thread(target=worker, name="persistent-statistics", daemon=True)
        self.thread.start()

    def finish(self, clean=True):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.store.finish_session(self.session["id"], clean)
        if not self.thread or not self.thread.is_alive():
            self.store.close()


def persistent_activity(activity, status, capital=0):
    """Keep evaluation windows separate from the cumulative reporting account."""
    if not status or not status.get("persistent") or "totals" not in status:
        return activity
    totals = status["totals"]
    result = dict(activity)
    for key in ("n", "wins", "losses", "grow", "loss", "realized"):
        result[key] = number(totals.get(key))
    result["pnl"] = result["realized"] + number(activity.get("unrealized"))
    recent_notional = sum(abs(number(getattr(c, "qty", 0)) * number(getattr(c, "entry", 0))) for c in activity.get("closes", []))
    open_notional = max(0, number(activity.get("tradedNotional")) - recent_notional)
    result["tradedNotional"] = number(totals.get("tradedNotional")) + open_notional
    result["pnlPct"] = result["pnl"] / result["tradedNotional"] * 100 if result["tradedNotional"] else 0
    base = number(totals.get("capital")) or number(capital)
    peak = base + number(totals.get("peakPnl"))
    current_dd = max(0, peak - (base + result["pnl"]))
    result["drawdownAmount"] = max(number(totals.get("maxDrawdown")), current_dd)
    result["drawdownPct"] = max(number(totals.get("maxDrawdownPct")), current_dd / peak * 100 if peak > 0 and base > 0 else 0)
    result["drawdownAvailable"] = base > 0
    result["drawdownBasis"] = "persistent-system-totals-plus-current-mark / starting-capital"
    result["source"] = "persistent-system-orders"
    return result
