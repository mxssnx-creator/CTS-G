"""Real in-memory SQLite, durable crash recovery and cross-process maintenance.

All fills are synthetic. No exchange client, real database or service is used.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/pulse"))
from runtime_statistics import StatisticsStore, RuntimeMonitor, lane_directory, read_status
from sqlite_memory import (MemoryStatisticsStore, DiskStatisticsStore,
                           JOURNAL_LIMIT, RECORD_LIMIT, RPC_LIMIT, memory_request, _write_message)

LANE = "bingx-x02"

HTTP_REQUEST = """
import io, json, os, sys
import pulse_http as ph
ph.DIR = os.environ['QA_ROOT']
h = ph.Handler.__new__(ph.Handler)
h.path = '/system.json?conn=vst'
blob = json.dumps(json.load(sys.stdin)).encode()
h.headers = {'Content-Length': str(len(blob)), 'Host': '127.0.0.1:3015'}
h.rfile = io.BytesIO(blob)
h._json = lambda obj, code=200: print(json.dumps([code, obj]))
h.do_POST()
"""


def fill(i, **changes):
    return dict(t=changes.pop("t", time.time() - 10 + i / 100000), conn=LANE,
                symbol="TEST-USDT", side="LONG", qty=1, entry=100,
                pnl=2 if i % 2 else -1, fee_total=.1, exchange_confirmed=True,
                ours=True, close_fill_id=f"ram-fill-{i}", **changes)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def store(self, connection=LANE, root=None, **settings):
        store = MemoryStatisticsStore(root or self.root, connection, settings)
        self.addCleanup(store.close)
        return store

    def child(self, code, data=None, *, expected=0):
        env = {**os.environ, "QA_ROOT": self.root, "PYTHONPATH": sys.path[0]}
        proc = subprocess.run([sys.executable, "-c", code], env=env, input=json.dumps(data).encode(),
                              capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, expected, proc.stderr.decode())
        return json.loads(proc.stdout) if proc.stdout else None

    def remote(self, body):
        return self.child("""
import json, os, sys
from sqlite_memory import memory_request
print(json.dumps(memory_request(os.environ['QA_ROOT'], 'bingx-x02', json.load(sys.stdin))))
""", body)

    def crash(self, rows, extra=""):
        self.child("""
import json, os, sys
from sqlite_memory import MemoryStatisticsStore
s = MemoryStatisticsStore(os.environ['QA_ROOT'], 'bingx-x02')
s.begin_session()
for row in json.load(sys.stdin): s.record_trade(row, 1000)
""" + extra + "\nos._exit(7)\n", rows, expected=7)

    def disk_totals(self, path):
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            row = db.execute("SELECT value FROM meta WHERE key='totals'").fetchone()
            return json.loads(row[0]) if row else {}

    def test_actual_ram_connection_and_latest_http_status(self):
        store = self.store()
        self.assertEqual(store.db.execute("PRAGMA database_list").fetchone()[2], "")
        self.assertEqual(store.db.execute("PRAGMA journal_mode").fetchone()[0], "memory")
        self.assertEqual(store.db.execute("PRAGMA temp_store").fetchone()[0], 2)
        store.record_trade(fill(1))
        self.assertEqual(self.disk_totals(store.path), {})  # No checkpoint yet.
        self.assertEqual(read_status(self.root, LANE)["totals"]["n"], 1)
        response = self.remote({"action": "status"})
        self.assertEqual(response["storageMode"], "memory")
        self.assertEqual(response["totals"]["n"], 1)
        self.assertGreater(response["memoryBytes"], 0)
        self.assertGreater(response["journalBytes"], 0)
        self.assertFalse(response["memoryRestartRequired"])

    def test_wal_database_migrates_without_losing_totals(self):
        with closing(StatisticsStore(self.root, LANE)) as old:
            for i in range(40): old.record_trade(fill(i), 1000)
            expected = old.status()["totals"]
        store = self.store()
        self.assertEqual(store.status()["totals"], expected)
        self.assertEqual(self.disk_totals(store.path), expected)
        self.assertFalse(Path(str(store.path) + "-wal").exists())
        with closing(sqlite3.connect(store.path)) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_process_crash_replays_once_and_counts_unclean_stop(self):
        rows = [fill(i) for i in range(500)]
        self.crash(rows)
        path = lane_directory(self.root, LANE) / "statistics.sqlite3"
        self.assertEqual(self.disk_totals(path), {})
        self.assertEqual(read_status(self.root, LANE)["storageMode"], "checkpoint")
        store = self.store()
        self.assertEqual(store.status()["totals"]["n"], 500)
        self.assertEqual(store.status()["totals"]["realized"], 250)
        for row in rows: self.assertFalse(store.record_trade(row))
        session = store.begin_session()
        self.assertEqual(store.status()["counters"]["crashes"], 1)
        self.assertEqual(store.status()["counters"]["recoveries"], 1)
        store.finish_session(session["id"])
        store.close()
        reopened = self.store()
        reopened.begin_session()
        self.assertEqual(reopened.status()["totals"]["n"], 500)
        self.assertEqual(reopened.status()["counters"]["crashes"], 1)

    def test_crash_after_journal_fsync_before_ram_commit(self):
        self.child("""
import json, os, sys
from sqlite_memory import MemoryStatisticsStore
s = MemoryStatisticsStore(os.environ['QA_ROOT'], 'bingx-x02')
s.db._commit_ram = lambda: os._exit(7)
s.record_trade(json.load(sys.stdin), 1000)
""", fill(1), expected=7)
        store = self.store()
        self.assertEqual(store.status()["totals"]["n"], 1)
        self.assertFalse(store.record_trade(fill(1)))

    def test_ram_commit_failure_replays_before_read_write_and_checkpoint(self):
        store = self.store()
        with patch.object(store.db, "_commit_ram", side_effect=sqlite3.OperationalError("injected commit failure")):
            self.assertTrue(store.record_trade(fill(1)))  # Durable commit is acknowledged.
        self.assertTrue(store.db.recovery_required)
        self.assertEqual(store.status()["totals"]["n"], 1)
        self.assertFalse(store.db.recovery_required)
        self.assertFalse(store.record_trade(fill(1)))
        store.record_trade(fill(2))
        store.checkpoint(force=True)
        self.assertEqual(self.disk_totals(store.path)["n"], 2)

    def test_durable_sample_is_acknowledged_once_after_ram_commit_failure(self):
        store = self.store()
        with patch.object(store.db, "_commit_ram", side_effect=sqlite3.OperationalError("injected commit failure")):
            store.sample({"sampledAt": time.time()}, 5, 12, 1, 2)
        self.assertEqual(store.status()["counters"], {"totalRunS": 5, "requests": 12, "errors": 1, "recoveries": 2})
        store.sample({"sampledAt": time.time()}, 5, 3, 0, 0)
        self.assertEqual(store.status()["counters"]["requests"], 15)

    def test_journal_fsync_failure_rolls_back_ram_then_retries(self):
        store = self.store()
        with patch("sqlite_memory.os.fsync", side_effect=OSError("injected fsync failure")):
            with self.assertRaises(OSError): store.record_trade(fill(1))
        self.assertEqual(store.status()["totals"], {})
        self.assertEqual(store.journal_path.stat().st_size, 0)
        self.assertTrue(store.record_trade(fill(1)))
        store.close()
        self.assertEqual(self.store().status()["totals"]["n"], 1)

    def test_failed_snapshot_preserves_previous_file_and_redo(self):
        store = self.store()
        original = store.path.read_bytes()
        store.record_trade(fill(1))
        journal = store.journal_path.read_bytes()
        with patch("sqlite_memory.os.replace", side_effect=OSError("injected snapshot failure")):
            with self.assertRaises(OSError): store.checkpoint(force=True)
        self.assertEqual(store.path.read_bytes(), original)
        self.assertEqual(store.journal_path.read_bytes(), journal)
        self.assertEqual(store.status()["checkpointError"], "OSError")
        self.assertEqual(list(store.directory.glob("*.tmp")), [])
        store.record_trade(fill(2))
        store.checkpoint(force=True)
        self.assertEqual(store.journal_path.stat().st_size, 0)
        self.assertEqual(self.disk_totals(store.path)["n"], 2)
        self.assertEqual(store.status()["checkpointError"], "")

    def test_crash_after_checkpoint_replace_before_journal_truncate(self):
        self.crash([fill(1), fill(2)], """
original_snapshot = s._snapshot
def crash_after_replace(target):
    original_snapshot(target)
    os._exit(7)
s._snapshot = crash_after_replace
s.checkpoint(force=True)
""")
        store = self.store()
        self.assertEqual(store.status()["totals"]["n"], 2)
        self.assertFalse(store.record_trade(fill(1)))
        self.assertFalse(store.record_trade(fill(2)))

    def test_partial_append_is_discarded_and_complete_corruption_is_reported(self):
        self.crash([fill(1)])
        journal = lane_directory(self.root, LANE) / "statistics.redo"
        with journal.open("ab") as stream: stream.write(b"incomplete final append")
        store = self.store()
        self.assertEqual(store.status()["totals"]["n"], 1)
        store.close()
        damaged = b"0" * 64 + b" [99,[]]\n"
        journal.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, "checksum"):
            MemoryStatisticsStore(self.root, LANE)
        self.assertEqual(journal.read_bytes(), damaged)
        self.assertEqual(self.disk_totals(store.path)["n"], 1)

    def test_sequence_gap_never_silently_discards_a_transaction(self):
        store = self.store()
        store.close()
        payload = json.dumps([2, []]).encode()
        damaged = hashlib.sha256(payload).hexdigest().encode() + b" " + payload + b"\n"
        store.journal_path.write_bytes(damaged)
        with self.assertRaisesRegex(ValueError, "sequence gap"):
            MemoryStatisticsStore(self.root, LANE)
        self.assertEqual(store.journal_path.read_bytes(), damaged)

    def test_late_journal_corruption_rolls_back_the_entire_disk_recovery(self):
        self.crash([fill(i) for i in range(100)])
        directory = lane_directory(self.root, LANE)
        journal = directory / "statistics.redo"
        with journal.open("ab") as stream: stream.write(b"0" * 64 + b" [102,[]]\n")
        evidence = journal.read_bytes()
        with self.assertRaisesRegex(ValueError, "checksum"):
            MemoryStatisticsStore(self.root, LANE)
        self.assertEqual(self.disk_totals(directory / "statistics.sqlite3"), {})
        self.assertEqual(journal.read_bytes(), evidence)

    def test_parallel_fills_and_independent_owners(self):
        store = self.store()
        other = self.store("bingx-x01")
        isolated = self.store(root=str(Path(self.root) / "other-project"))
        rows = [fill(i) for i in range(500)]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(store.record_trade, rows * 3))
        self.assertEqual(sum(results), 500)
        self.assertEqual(store.status()["totals"]["n"], 500)
        self.assertEqual(store.status()["totals"]["realized"], 250)
        self.assertEqual(other.status()["totals"], {})
        self.assertEqual(isolated.status()["totals"], {})
        for cls in (MemoryStatisticsStore, DiskStatisticsStore):
            with self.assertRaises(BlockingIOError): cls(self.root, LANE)
        self.assertFalse(store.record_trade({**fill(1000), "conn": "bingx-x01"}))
        self.assertFalse(store.record_trade({**fill(1001), "exchange_confirmed": False}))

    def test_http_process_backup_and_reset_target_the_running_owner(self):
        store = self.store()
        other = self.store("bingx-x01")
        other.record_trade({**fill(3), "conn": "bingx-x01"})
        store.record_trade(fill(1))
        store.increment("requests", 5)
        sentinel = Path(self.root) / "open-bingx-x02.json"
        sentinel.write_text('{"untouched":true}')

        status, result = self.child(HTTP_REQUEST, {"action": "backup"})
        self.assertEqual(status, 200, result)
        self.assertEqual(self.disk_totals(store.directory / "backups" / result["backup"])["n"], 1)
        self.assertEqual(self.disk_totals(store.path), {})
        status, result = self.child(HTTP_REQUEST, {"action": "reset", "scope": "telemetry", "confirmation": f"RESET TELEMETRY {LANE}"})
        self.assertEqual(status, 200, result)
        self.assertEqual(store.status()["counters"], {})
        self.assertEqual(store.status()["totals"]["n"], 1)
        status, result = self.child(HTTP_REQUEST, {"action": "reset", "scope": "statistics", "confirmation": f"RESET STATISTICS {LANE}"})
        self.assertEqual(status, 200, result)
        self.assertEqual(store.status()["totals"], {})
        self.assertFalse(store.record_trade(fill(1)))
        self.assertEqual(other.status()["totals"]["n"], 1)
        self.assertEqual(sentinel.read_text(), '{"untouched":true}')

    def test_reset_journal_survives_crash_before_checkpoint(self):
        self.crash([fill(1)], """
s.sample({'sampledAt': 1}, 1, 2, 0, 0)
s.record_events([{'event_id': 'old-event', 'ts': 1}])
s.checkpoint(force=True)
s._snapshot = lambda target: None if target.name != 'statistics.sqlite3' else os._exit(7)
s.reset('statistics', 'RESET STATISTICS bingx-x02')
""")
        store = self.store()
        self.assertEqual(store.status()["totals"], {})
        self.assertEqual(store.status()["counters"], {})
        self.assertEqual(store.status()["dbRows"]["trades"], 0)
        self.assertEqual(store.status()["dbRows"]["samples"], 0)
        self.assertEqual(store.status()["dbRows"]["events"], 0)
        self.assertFalse(store.record_trade(fill(1)))

    def test_http_backup_of_stopped_owner_recovers_unsnapshotted_financial_records(self):
        self.crash([fill(1), fill(2)])
        status, result = self.child(HTTP_REQUEST, {"action": "backup"})
        self.assertEqual(status, 200, result)
        directory = lane_directory(self.root, LANE)
        self.assertEqual(self.disk_totals(directory / "backups" / result["backup"])["n"], 2)
        self.assertEqual((directory / "statistics.redo").stat().st_size, 0)
        self.assertEqual(self.store().status()["totals"]["n"], 2)

    def test_expired_and_old_owner_requests_cannot_reset_new_data(self):
        store = self.store()
        old_owner = json.loads((store.directory / "owner.json").read_text())["id"]
        store.close()
        store = self.store()
        store.record_trade(fill(1))
        body = {"action": "reset", "scope": "statistics", "confirmation": f"RESET STATISTICS {LANE}"}
        _write_message(store.directory / "request.json", {"id": "obsolete", "owner": old_owner,
                       "expiresAt": time.time() + 10, "body": body})
        time.sleep(.075)
        self.assertEqual(store.status()["totals"]["n"], 1)
        owner = json.loads((store.directory / "owner.json").read_text())["id"]
        _write_message(store.directory / "request.json", {"id": "expired", "owner": owner,
                       "expiresAt": time.time() - 1, "body": body})
        deadline = time.monotonic() + 2
        while not (store.directory / "response.json").exists() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(json.loads((store.directory / "response.json").read_text())["errorType"], "TimeoutError")
        self.assertEqual(store.status()["totals"]["n"], 1)
        with self.assertRaises(ValueError): _write_message(store.directory / "request.json", {"large": "x" * RPC_LIMIT})

    def test_parallel_http_clients_are_serialized_without_dropped_responses(self):
        store = self.store(systemBackupKeep=2)
        store.record_trade(fill(1))
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(self.remote, [{"action": "status"}] * 12))
        self.assertTrue(all(r["totals"]["n"] == 1 for r in results))
        for _ in range(4): self.remote({"action": "backup"})
        self.assertEqual(len(list((store.directory / "backups").glob("*.sqlite3"))), 2)
        self.assertLessEqual(len(list(store.directory.iterdir())), 10)
        for name in ("owner.json", "request.json", "response.json"):
            self.assertLessEqual((store.directory / name).stat().st_size, RPC_LIMIT)

    def test_row_and_byte_caps_keep_newest_details_and_lifetime_totals(self):
        store = self.store(systemTradeMaxRows=250, systemDbMaxMb=8)
        base = time.time() - 20
        for i in range(1800): store.record_trade(fill(i, t=base + i / 10000, detail="x" * 12000))
        store.maintain()
        result = store.status()
        self.assertEqual(result["totals"]["n"], 1800)
        self.assertEqual(result["totals"]["realized"], 900)
        self.assertLessEqual(result["dbRows"]["trades"], 250)
        self.assertLessEqual(result["memoryBytes"], 8 * 1048576)
        self.assertLessEqual(result["journalBytes"], JOURNAL_LIMIT)
        newest = store.db.execute("SELECT id FROM trades ORDER BY t DESC LIMIT 1").fetchone()[0]
        self.assertEqual(newest, "ram-fill-1799")
        expected = result["totals"]
        store.close()
        self.assertEqual(self.store(systemDbMaxMb=8).status()["totals"], expected)

    def test_metadata_only_writes_checkpoint_before_journal_capacity(self):
        store = self.store()
        # A smaller real journal budget exercises the identical production
        # boundary without generating megabytes of unrelated runtime samples.
        with patch("sqlite_memory.JOURNAL_LIMIT", RECORD_LIMIT + 4096):
            for _ in range(500): store.increment("requests", 1)
            self.assertLess(store.journal_path.stat().st_size, RECORD_LIMIT + 4096)
        self.assertEqual(store.status()["counters"]["requests"], 500)
        with closing(sqlite3.connect(store.path)) as db:
            self.assertGreater(json.loads(db.execute("SELECT value FROM meta WHERE key='counters'").fetchone()[0])["requests"], 0)

    def test_checkpoint_interval_and_unchanged_settings_do_not_rewrite_journal(self):
        store = self.store(systemSqliteCheckpointS=20)
        before = store.journal_path.read_bytes()
        store.configure({"systemSqliteCheckpointS": 20})
        self.assertEqual(store.journal_path.read_bytes(), before)
        store.record_trade(fill(1))
        with patch("sqlite_memory.time.monotonic", return_value=store._checkpoint_mono + 19):
            self.assertFalse(store.checkpoint())
        self.assertEqual(self.disk_totals(store.path), {})
        with patch("sqlite_memory.time.monotonic", return_value=store._checkpoint_mono + 21):
            self.assertTrue(store.checkpoint())
        self.assertEqual(self.disk_totals(store.path)["n"], 1)

    def test_checkpoint_runs_independently_of_metrics_interval_and_stops_when_idle(self):
        store = self.store(systemSqliteCheckpointS=1, systemMetricsIntervalS=60)
        store.record_trade(fill(1))
        deadline = time.monotonic() + 3
        while store.journal_path.stat().st_size and time.monotonic() < deadline:
            time.sleep(.025)
        self.assertEqual(self.disk_totals(store.path)["n"], 1)
        before = store.path.stat().st_mtime_ns
        store._checkpoint_mono -= 2
        self.assertFalse(store.checkpoint())
        self.assertEqual(store.path.stat().st_mtime_ns, before)

    def test_disk_mode_after_crash_recovers_journal_and_mode_switch_waits_for_restart(self):
        self.crash([fill(1), fill(2)])
        monitor = RuntimeMonitor(self.root, LANE, {"systemSqliteMemory": 0})
        self.addCleanup(monitor.finish)
        self.assertEqual(monitor.store.status()["storageMode"], "disk")
        self.assertEqual(monitor.store.status()["totals"]["n"], 2)
        self.assertFalse(monitor.store.record_trade(fill(1)))
        self.assertEqual(self.remote({"action": "status"})["storageMode"], "disk")
        monitor.configure({"systemSqliteMemory": 1})
        self.assertTrue(monitor.store.status()["memoryRestartRequired"])
        self.assertEqual(monitor.store.status()["storageMode"], "disk")
        monitor.finish()
        self._cleanups.pop()  # Already finished this monitor; no duplicate finish.
        store = self.store()
        self.assertEqual(store.status()["totals"]["n"], 2)
        store.configure({"systemSqliteMemory": 0})
        self.assertTrue(store.status()["memoryRestartRequired"])
        self.assertEqual(store.status()["storageMode"], "memory")

    def test_large_old_database_shrinks_before_ram_import(self):
        with closing(StatisticsStore(self.root, LANE, {"systemDbMaxMb": 64})) as old:
            for i in range(950): old.record_trade(fill(i, detail="x" * 12000))
            self.assertGreater(old.db.execute("PRAGMA page_count").fetchone()[0] * 4096, 8 * 1048576)
        store = self.store(systemDbMaxMb=8)
        self.assertEqual(store.status()["totals"]["n"], 950)
        self.assertLessEqual(store.status()["memoryBytes"], 8 * 1048576)
        self.assertEqual(store.db.execute("SELECT id FROM trades ORDER BY t DESC LIMIT 1").fetchone()[0], "ram-fill-949")


if __name__ == "__main__":
    unittest.main()
