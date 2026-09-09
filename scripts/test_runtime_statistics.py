"""Real SQLite restart, retention, reset and resource-accounting tests, offline."""
import json
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))
from runtime_statistics import StatisticsStore, RuntimeMonitor, lane_directory, read_status, persistent_activity
import runtime_statistics as runtime_stats
from system_settings import calculation_overlay, normalize_system_settings
from set_engine import SetBook
from storage_paths import append_bounded_line, configure_retention
from event_ledger import EventLedger
import pulse_http as ph


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.now = time.time() - 20

    def store(self, lane="bingx-x02", **settings):
        store = StatisticsStore(self.root, lane, settings)
        self.addCleanup(store.close)
        return store

    def trade(self, i, **kwargs):
        return dict(t=self.now + i / 10000, conn="bingx-x02", symbol="TEST-USDT", side="LONG",
                    qty=1, entry=100, pnl=2 if i % 2 else -1, pnl_pct=.002, fee_total=.1,
                    exchange_confirmed=True, ours=True, close_fill_id=f"close-{i}", **kwargs)

    def test_thousands_of_trades_survive_retention_and_restart_without_double_count(self):
        store = self.store(systemTradeMaxRows=250, systemDbMaxMb=8)
        for i in range(2500):
            store.record_trade(self.trade(i), capital=1000)
        store.maintain()
        status = store.status()
        self.assertLessEqual(status["dbRows"]["trades"], 250)
        self.assertEqual(status["totals"]["n"], 2500)
        self.assertEqual(status["totals"]["realized"], 1250)
        self.assertAlmostEqual(status["totals"]["fees"], 250)
        reopened = self.store()
        for i in range(2500):
            self.assertFalse(reopened.record_trade(self.trade(i)))
        self.assertEqual(reopened.status()["totals"], status["totals"])

    def test_byte_ceiling_bounds_large_details_and_preserves_totals(self):
        store = self.store(systemDbMaxMb=8, systemTradeMaxRows=100000)
        for i in range(1400):
            store.record_trade(self.trade(i, detail="e" * 12000))
        store.maintain()
        status = store.status()
        self.assertEqual(status["totals"]["n"], 1400)
        self.assertLessEqual(store.path.stat().st_size, 8 * 1048576)
        self.assertLessEqual(status["dbBytes"], 9 * 1048576)
        self.assertLess(status["dbRows"]["trades"], 1400)

    def test_cross_connection_and_unconfirmed_never_change_totals(self):
        store = self.store()
        row = self.trade(1)
        for changes in ({"conn": "bingx-x01"}, {"ours": False}, {"exchange_confirmed": False}):
            self.assertFalse(store.record_trade({**row, **changes}))
        self.assertEqual(store.status()["totals"], {})
        store.record_trade(row)
        self.assertEqual(self.store("bingx-x01").status()["totals"], {})

    def test_partial_fill_identity_and_reporting_are_not_limited_to_recent_tape(self):
        store = self.store(systemTradeMaxRows=250)
        for i in range(500):
            store.record_trade(self.trade(i, partial=bool(i % 2), client_id=f"parent-{i//2}"))
        store.maintain()
        act = persistent_activity({"closes": [], "tradedNotional": 100, "unrealized": -10}, store.status(), 1000)
        self.assertEqual(act["n"], 500)
        self.assertEqual(act["realized"], 250)
        self.assertEqual(act["pnl"], 240)
        self.assertEqual(act["tradedNotional"], 50100)
        self.assertGreaterEqual(act["drawdownAmount"], 10)

    def test_backup_restores_and_rotates(self):
        store = self.store(systemBackupKeep=2)
        for i in range(12): store.record_trade(self.trade(i))
        for _ in range(4): name = store.backup()
        files = list((store.directory / "backups").glob("*.sqlite3"))
        self.assertEqual(len(files), 2)
        db = sqlite3.connect(store.directory / "backups" / name)
        try:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            totals = json.loads(db.execute("SELECT value FROM meta WHERE key='totals'").fetchone()[0])
            self.assertEqual(totals["n"], 12)
        finally: db.close()

    def test_reset_is_scoped_backed_up_and_does_not_reimport_the_old_tape(self):
        store = self.store()
        other = self.store("bingx-x01")
        other.record_trade({**self.trade(1), "conn": "bingx-x01"})
        sentinels = {}
        for name in ("open-bingx-x02.json", "pending-bingx-x02.json", "history-1m-bingx-x02.json", "overlay-bingx-x02.json"):
            path = Path(self.root) / name
            path.write_text('{"preserve":true}')
            sentinels[path] = path.read_bytes()
        for i in range(250): store.record_trade(self.trade(i))
        store.increment("recoveries", 3)
        with self.assertRaises(ValueError): store.reset("statistics", "RESET STATISTICS bingx-x01")
        self.assertFalse((store.directory / "backups").exists())
        store.reset("telemetry", "RESET TELEMETRY bingx-x02")
        self.assertEqual(store.status()["totals"]["n"], 250)
        self.assertEqual(store.status()["counters"], {})
        result = store.reset("statistics", "RESET STATISTICS bingx-x02")
        self.assertTrue(result["backup"])
        for i in range(250): self.assertFalse(store.record_trade(self.trade(i)))
        self.assertEqual(store.status()["totals"], {})
        self.assertEqual(other.status()["totals"]["n"], 1)
        for path, content in sentinels.items(): self.assertEqual(path.read_bytes(), content)
        store.record_trade({**self.trade(1000), "t": time.time() + .01})
        self.assertEqual(store.status()["totals"]["n"], 1)

    def test_real_process_crash_and_clean_restart_are_distinguished(self):
        code = "from runtime_statistics import StatisticsStore; import os; s=StatisticsStore(os.environ['QA_ROOT'],'bingx-x02'); s.begin_session(); os._exit(7)"
        env = {**os.environ, "QA_ROOT": self.root, "PYTHONPATH": sys.path[0]}
        result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True)
        self.assertEqual(result.returncode, 7, result.stderr.decode())
        store = self.store()
        session = store.begin_session()
        self.assertEqual(store.status()["counters"]["crashes"], 1)
        self.assertEqual(store.status()["counters"]["recoveries"], 1)
        store.finish_session(session["id"])
        store.begin_session()
        self.assertEqual(store.status()["counters"]["crashes"], 1)

    def test_request_rate_and_recovery_count_use_deltas_and_survive_reset(self):
        monitor = RuntimeMonitor(self.root, "bingx-x02")
        self.addCleanup(monitor.finish)
        monitor.last_mono = monitor.started = 100
        monitor.last_cpu = 1
        pulse = NS(api=NS(stats={"rest": 30}), errors=2, cycle=4, closed=[])
        monitor.note_failure(); monitor.note_failure(); monitor.note_success(); monitor.note_success()
        with patch("runtime_statistics.time.monotonic", return_value=110), patch("runtime_statistics.time.process_time", return_value=2):
            result = monitor.sample(pulse)
        self.assertAlmostEqual(result["requestsPerSec"], 3)
        self.assertAlmostEqual(result["cpuPct"], 10)
        self.assertEqual(result["counters"]["recoveries"], 1)
        monitor.store.reset("telemetry", "RESET TELEMETRY bingx-x02")
        pulse.api.stats["rest"] = 40
        with patch("runtime_statistics.time.monotonic", return_value=115), patch("runtime_statistics.time.process_time", return_value=2.5):
            result = monitor.sample(pulse)
        self.assertEqual(result["requestsPerSec"], 2)
        self.assertEqual(result["counters"]["requests"], 10)
        self.assertEqual(result["counters"]["recoveries"], 0)

    def test_live_readers_and_parallel_writers_do_not_leak_handles(self):
        store = self.store()
        def job(i):
            store.record_trade(self.trade(i))
            return read_status(self.root, "bingx-x02")
        with ThreadPoolExecutor(max_workers=8) as pool: results = list(pool.map(job, range(250)))
        self.assertTrue(all(r.get("persistent") for r in results))
        self.assertEqual(store.status()["totals"]["n"], 250)
        if Path("/proc/self/fd").exists():
            before = len(list(Path("/proc/self/fd").iterdir()))
            for _ in range(100): read_status(self.root, "bingx-x02")
            self.assertLessEqual(len(list(Path("/proc/self/fd").iterdir())), before + 1)

    def test_status_reader_allows_a_busy_live_owner_to_return_ram_status(self):
        captured = {}

        def owner_status(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return {"connection": "bingx-x02", "storageMode": "memory", "persistent": True}

        with patch("sqlite_memory.memory_request", side_effect=owner_status):
            result = read_status(self.root, "bingx-x02")
        self.assertEqual(captured["timeout"], 2.0)
        self.assertEqual(result["storageMode"], "memory")

    def test_log_and_event_payloads_are_bounded(self):
        path = str(Path(self.root) / "engine.log")
        configure_retention(path, 32, 4096)
        for i in range(200): append_bounded_line(path, f"{i}:" + "x" * 500)
        self.assertLessEqual(Path(path).stat().st_size, 4096)
        self.assertLessEqual(len(Path(path).read_text().splitlines()), 32)
        ledger = EventLedger(str(Path(self.root) / "events.json"), "bingx-x02", flush_interval_s=5)
        ledger.record("evaluation", "big", metadata={"oversized": ["secret-free-test"] * 100000})
        ledger.flush()
        self.assertLess(Path(ledger.path).stat().st_size, 20000)

    def test_corrupt_database_is_reported_without_deleting_anything(self):
        directory = lane_directory(self.root, "bingx-x02"); directory.mkdir(parents=True)
        path = directory / "statistics.sqlite3"; path.write_bytes(b"not a database")
        result = read_status(self.root, "bingx-x02")
        self.assertFalse(result["persistent"])
        self.assertIn("error", result)
        self.assertEqual(path.read_bytes(), b"not a database")

    def test_internal_general_catalog_ignores_execution_switches(self):
        catalogs = []
        for normal in (False, True):
            for active in (False, True):
                overlay = calculation_overlay({"normalExecutionEnabled": normal, "blockActive": active,
                    "stratGeneral": False, "histEnabled": False, "modules": {"core.historic": False},
                    "setMinStep": 3, "setStepMax": 3, "slToTpRatios": [.6], "stratTrailing": False})
                book = SetBook(); book.load(overlay)
                self.assertTrue(book.enabled)
                self.assertIn("general", book.packs)
                self.assertEqual(overlay["normalExecutionEnabled"], normal)
                self.assertEqual(overlay["blockActive"], active)
                catalogs.append(set(book.sets))
        self.assertTrue(all(c == catalogs[0] for c in catalogs))
        self.assertGreater(len(catalogs[0]), 0)

    def test_nonfinite_settings_and_zero_automatic_memory_are_safe(self):
        settings = normalize_system_settings({"rssSoftMb": 0, "rssHardMb": 0, "systemWorkers": float("inf"), "systemOrderRps": -1})
        self.assertEqual(settings["rssSoftMb"], 0)
        self.assertEqual(settings["rssHardMb"], 0)
        self.assertEqual(settings["systemWorkers"], 2)
        self.assertEqual(settings["systemOrderRps"], .5)
        self.assertEqual(calculation_overlay({"blockActiveMinLevel": 999, "blockMaxStack": 3})["blockActiveMinLevel"], 3)
        with self.assertRaises(ValueError): lane_directory(self.root, "../other")

    def test_http_maintenance_validates_connection_origin_scope_and_exact_confirmation(self):
        store = self.store(); store.record_trade(self.trade(1))
        def request(conn, body, origin="", method="POST", path="/system.json"):
            handler = ph.Handler.__new__(ph.Handler)
            handler.path = f"{path}?conn={conn}"
            blob = json.dumps(body).encode()
            handler.headers = {"Content-Length": str(len(blob)), "Host": "127.0.0.1:3015", "Origin": origin}
            handler.rfile = io.BytesIO(blob)
            result = []
            handler._json = lambda obj, code=200: result.append((code, obj))
            with patch.object(ph, "DIR", self.root):
                if method == "GET": handler.do_GET()
                else: handler.do_POST()
            return result[0]
        self.assertEqual(request("overall", {"action":"reset"})[0], 400)
        self.assertEqual(request("vst", {"action":"reset"}, "https://foreign.example")[0], 403)
        self.assertEqual(request("vst", {"action":"reset", "scope":"all", "confirmation":"RESET ALL bingx-x02"})[0], 400)
        self.assertEqual(request("vst", {"action":"reset", "scope":"statistics", "confirmation":"RESET STATISTICS bingx-x01"})[0], 400)
        self.assertEqual(store.status()["totals"]["n"], 1)
        self.assertEqual(request("vst", {}, method="GET", path="/%73tatistics/bingx-x02/statistics.sqlite3")[0], 404)
        self.assertEqual(request("vst", {"action":"compact"})[0], 200)
        self.assertEqual(request("vst", {"action":"reset", "scope":"statistics", "confirmation":"RESET STATISTICS bingx-x02"})[0], 200)
        self.assertEqual(store.status()["totals"], {})

    def test_database_failure_retries_confirmed_journal_without_duplicate_totals(self):
        monitor = RuntimeMonitor(self.root, "bingx-x02")
        self.addCleanup(monitor.finish)
        row = self.trade(1)
        with patch.object(monitor.store, "record_trade", side_effect=sqlite3.OperationalError("busy")):
            self.assertFalse(monitor.record_trade(row))
            self.assertTrue(monitor.last_error)
        pulse = NS(api=NS(stats={"rest":0}), errors=0, cycle=1, closed=[row], start_eq=1000)
        monitor.sample(pulse)
        monitor.sample(pulse)
        self.assertEqual(monitor.snapshot["totals"]["n"], 1)
        self.assertEqual(monitor.last_error, "")

    def test_http_backup_waits_for_short_engine_write_without_losing_totals(self):
        store = self.store()
        store.record_trade(self.trade(1))
        store.db.execute("BEGIN IMMEDIATE")
        handler = ph.Handler.__new__(ph.Handler)
        handler.path = "/system.json?conn=vst"
        blob = json.dumps({"action": "backup"}).encode()
        handler.headers = {"Content-Length": str(len(blob)), "Host": "127.0.0.1:3015"}
        handler.rfile = io.BytesIO(blob)
        result, timers = [], []
        handler._json = lambda obj, code=200: result.append((code, obj))

        def open_while_engine_writes(*args, **kwargs):
            timer = threading.Timer(.35, store.db.commit)
            timers.append(timer)
            timer.start()
            return StatisticsStore(*args, **kwargs)

        try:
            with patch.object(ph, "DIR", self.root), patch.object(ph, "StatisticsStore", side_effect=open_while_engine_writes):
                handler.do_POST()
        finally:
            for timer in timers:
                timer.join()
            store.db.rollback()
        self.assertEqual(result[0][0], 200, result)
        backups = list((store.directory / "backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as copied:
            self.assertEqual(copied.execute("PRAGMA quick_check").fetchone()[0], "ok")
            totals = json.loads(copied.execute("SELECT value FROM meta WHERE key='totals'").fetchone()[0])
        self.assertEqual(totals["n"], 1)
        self.assertEqual(store.status()["totals"]["n"], 1)

    def test_request_buckets_apply_user_ceilings_and_keep_auto_memory_available(self):
        from bingx_fast import FastBingX, TokenBucket, LIMITS
        from load_engine import LoadGovernor
        api = FastBingX.__new__(FastBingX)
        api.buckets = {k: TokenBucket(*v) for k,v in LIMITS.items()}
        api.configure_limits({"systemPublicRps": 1, "systemPrivateRps": 2, "systemOrderRps": .5})
        self.assertEqual([api.buckets[k].rate for k in ("public", "private", "order")], [1,2,.5])
        self.assertTrue(all(b.tokens <= b.burst for b in api.buckets.values()))
        api.configure_limits({"systemOrderRps":999})
        self.assertEqual(api.buckets['order'].rate, LIMITS['order'][0])
        governor = LoadGovernor()
        governor.configure({"rssSoftMb":1000, "rssHardMb":2000})
        governor.configure({"rssSoftMb":0, "rssHardMb":0})
        self.assertEqual((governor.soft_mb, governor.hard_mb), (0,0))

    def test_redis_health_reports_bounded_database_breakdown_and_ops_rate(self):
        raw = "\n".join([
            "used_memory:1048576",
            "maxmemory:67108864",
            "instantaneous_ops_per_sec:17",
            "aof_enabled:1",
            "aof_last_write_status:ok",
            "rdb_last_bgsave_status:ok",
            "db0:keys=12,expires=3,avg_ttl=9000",
            "db2:keys=4,expires=1,avg_ttl=1200",
        ])
        result = NS(returncode=0, stdout=raw)
        runtime_stats._REDIS_CACHE.clear()
        try:
            with patch.object(runtime_stats.subprocess, "run", return_value=result):
                health = runtime_stats.redis_health()
        finally:
            runtime_stats._REDIS_CACHE.clear()
        self.assertTrue(health["available"])
        self.assertEqual(health["databaseCount"], 2)
        self.assertEqual(health["keys"], 16)
        self.assertEqual(health["expiringKeys"], 4)
        self.assertEqual(health["operationsPerSec"], 17)
        self.assertEqual(health["databases"]["db0"]["avgTtlMs"], 9000)


if __name__ == "__main__": unittest.main()
