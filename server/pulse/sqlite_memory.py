"""True SQLite :memory: statistics with one owner, redo journal and checkpoints.

Financial records and metadata are fsynced before their RAM transaction commits.
Event details / resource samples use periodic snapshots. No order execution lives
here. The HTTP process asks the owner; it never creates a second private RAM DB.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid

from runtime_statistics import StatisticsStore, lane_directory, MIB

JOURNAL_LIMIT = 4 * MIB
RECORD_LIMIT = 64 * 1024
RPC_LIMIT = 128 * 1024


_OWNERS = {}
_OWNERS_LOCK = threading.RLock()


def _write_message(path, value):
    payload = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > RPC_LIMIT:
        raise ValueError("statistics message too large")
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_message(path):
    with path.open("rb") as stream:
        raw = stream.read(RPC_LIMIT + 1)
    if len(raw) > RPC_LIMIT:
        raise ValueError("statistics message too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid statistics message")
    return value


@contextmanager
def owner_guard(root, connection):
    directory = lane_directory(root, connection)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / "owner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _allowed(sql):
    return sql.startswith(("INSERT INTO meta VALUES", "INSERT OR IGNORE INTO trades VALUES", "DELETE FROM trades"))


class DurableMemoryConnection(sqlite3.Connection):
    """Log the actual SQL deltas, including generated IDs and exact totals."""
    journal = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending = []
        self.before_transaction = None
        self.recovery_required = False

    def ensure_recovered(self):
        if not self.recovery_required:
            return
        journal, self.journal = self.journal, None
        before, self.before_transaction = self.before_transaction, None
        self.recovery_required = False
        try:
            _replay(self, journal)
        except BaseException:
            self.recovery_required = True
            raise
        finally:
            self.journal = journal
            self.before_transaction = before

    def execute(self, sql, parameters=(), /):
        self.ensure_recovered()
        cursor = super().execute(sql, parameters)
        if self.journal is not None and cursor.rowcount > 0 and _allowed(sql):
            if not (sql.startswith("INSERT INTO meta") and parameters[0] in ("latest", "sqliteCheckpointAt")):
                self.pending.append([sql, list(parameters)])
        return cursor

    def _commit_ram(self):
        super().commit()

    def commit(self):
        durable = False
        try:
            if self.pending:
                row = super().execute("SELECT value FROM meta WHERE key='sqliteJournalSeq'").fetchone()
                seq = int(json.loads(row[0])) + 1 if row else 1
                payload = json.dumps([seq, self.pending], separators=(",", ":"), allow_nan=False).encode()
                record = hashlib.sha256(payload).hexdigest().encode() + b" " + payload + b"\n"
                if len(record) > RECORD_LIMIT or self.journal.stat().st_size + len(record) > JOURNAL_LIMIT:
                    raise OSError("statistics redo journal is full; checkpoint required")
                with self.journal.open("ab") as stream:
                    offset = stream.tell()
                    try:
                        stream.write(record)
                        stream.flush()
                        os.fsync(stream.fileno())
                        durable = True
                    except BaseException:
                        stream.truncate(offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                        raise
                super().execute("INSERT INTO meta VALUES ('sqliteJournalSeq',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seq),))
            self._commit_ram()
        except BaseException as exc:
            self.rollback()
            # A durable transaction can outlive its RAM commit. Replay it before
            # any further read, write or checkpoint; never reuse its sequence.
            self.recovery_required = durable
            if not durable or not isinstance(exc, Exception):
                raise
            # fsync is the commit point. Acknowledge it so counters/samples are
            # not retried as fresh deltas after an already-durable transaction.
        finally:
            self.pending.clear()

    def rollback(self):
        self.pending.clear()
        super().rollback()

    def __enter__(self):
        self.ensure_recovered()
        if self.before_transaction and not self.in_transaction:
            self.before_transaction()
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def backup(self, *args, **kwargs):
        self.ensure_recovered()
        return super().backup(*args, **kwargs)


def _replay(db, journal):
    if not journal.exists() or not journal.stat().st_size:
        return
    if journal.stat().st_size > JOURNAL_LIMIT:
        raise ValueError("statistics redo journal exceeds its limit")
    row = db.execute("SELECT value FROM meta WHERE key='sqliteJournalSeq'").fetchone()
    applied = int(json.loads(row[0])) if row else 0
    valid_bytes = 0
    # The bounded replay is one SQLite transaction, including resets. This
    # avoids one disk fsync per historic fill and rolls back a corrupt replay.
    with db, journal.open("rb") as stream:
        while record := stream.readline(RECORD_LIMIT + 1):
            if len(record) > RECORD_LIMIT:
                raise ValueError("oversized statistics journal record")
            if not record.endswith(b"\n"):
                break  # Interrupted append: only the incomplete final line is discarded.
            checksum, payload = record[:-1].split(b" ", 1)
            if hashlib.sha256(payload).hexdigest().encode() != checksum:
                raise ValueError("statistics journal checksum mismatch")
            seq, operations = json.loads(payload)
            if not isinstance(seq, int) or not isinstance(operations, list) or any(not _allowed(sql) for sql, _ in operations):
                raise ValueError("invalid statistics journal transaction")
            if seq > applied:
                if seq != applied + 1:
                    raise ValueError("statistics journal sequence gap")
                for sql, params in operations:
                    db.execute(sql, params)
                db.execute("INSERT INTO meta VALUES ('sqliteJournalSeq',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(seq),))
                applied = seq
            valid_bytes += len(record)
        # A reset must invalidate checkpoint telemetry even when those tables
        # were already empty in RAM when the reset was committed.
        row = db.execute("SELECT value FROM meta WHERE key='telemetryResetAt'").fetchone()
        if row:
            reset_at = float(json.loads(row[0]))
            for table in ("samples", "events"):
                db.execute(f"DELETE FROM {table} WHERE t<=?", (reset_at,))
            latest = db.execute("SELECT value FROM meta WHERE key='latest'").fetchone()
            if latest and float(json.loads(latest[0]).get("sampledAt", 0)) <= reset_at:
                db.execute("DELETE FROM meta WHERE key='latest'")
        row = db.execute("SELECT value FROM meta WHERE key='statisticsResetAt'").fetchone()
        if row:
            db.execute("DELETE FROM trades WHERE t<=?", (float(json.loads(row[0])),))
    # Keep complete records until a verified checkpoint has covered them.
    if valid_bytes != journal.stat().st_size:
        with journal.open("r+b") as stream:
            stream.truncate(valid_bytes)
            stream.flush()
            os.fsync(stream.fileno())


def recover_durable_journal(root, connection, *, guarded=False):
    """Switching back to disk mode or maintaining a stopped owner replays first."""
    if not guarded:
        with owner_guard(root, connection):
            return recover_durable_journal(root, connection, guarded=True)
    directory = lane_directory(root, connection)
    journal = directory / "statistics.redo"
    if not journal.exists() or not journal.stat().st_size:
        return
    with closing(sqlite3.connect(f"file:{directory / 'statistics.sqlite3'}?mode=rw", uri=True, timeout=2)) as db:
        db.execute("PRAGMA synchronous=FULL")
        _replay(db, journal)
    with journal.open("wb") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def memory_request(root, connection, body, *, timeout=10):
    directory = lane_directory(root, connection).resolve()
    with _OWNERS_LOCK:
        owner = _OWNERS.get(str(directory))
    if owner is not None and owner.owner_pid == os.getpid():
        return owner._dispatch(body)
    try:
        owner_info = _read_message(directory / "owner.json")
        probe = os.open(directory / "owner.lock", os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return None  # Stale mailbox after an owner crash.
        except BlockingIOError:
            pass
    finally:
        os.close(probe)
    # One bounded mailbox per lane. Requests from separate HTTP workers cannot
    # overwrite each other. No sockets, ports, unbounded queue or pickle payloads.
    deadline = time.monotonic() + timeout
    fd = os.open(directory / "request.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("statistics owner is busy")
                time.sleep(0.005)
        request_id = uuid.uuid4().hex
        _write_message(directory / "request.json", {"id": request_id, "owner": owner_info["id"],
                      "expiresAt": time.time() + max(0, deadline - time.monotonic()), "body": body})
        while time.monotonic() < deadline:
            try:
                response = _read_message(directory / "response.json")
                if response.get("id") == request_id:
                    if response.get("error"):
                        if response.get("errorType") == "ValueError":
                            raise ValueError(response["error"])
                        raise OSError(response["error"])
                    return response["result"]
            except FileNotFoundError:
                pass
            time.sleep(0.005)
        raise TimeoutError("statistics owner response unavailable; check status before retrying")
    finally:
        os.close(fd)


class OwnerMixin:
    """Exactly one engine owner in either mode; HTTP maintenance uses its lock."""
    def __init__(self, root, connection, settings=None, *, timeout=0.2):
        self._guard = owner_guard(root, connection)
        self._guard.__enter__()
        self._closed = False
        self.owner_pid = os.getpid()
        self._rpc_stop = threading.Event()
        self._rpc_thread = None
        try:
            super().__init__(root, connection, settings, timeout=timeout)
            self._open_complete()
            self._start_rpc()
        except BaseException:
            self._rpc_stop.set()
            if self._rpc_thread:
                self._rpc_thread.join(timeout=2)
            if hasattr(self, "directory"):
                with _OWNERS_LOCK:
                    _OWNERS.pop(str(self.directory.resolve()), None)
                (self.directory / "owner.json").unlink(missing_ok=True)
            if hasattr(self, "db"):
                self.db.close()
            self._guard.__exit__(None, None, None)
            raise

    def _open_complete(self):
        pass

    def checkpoint(self, force=False):
        return False

    def _tick(self):
        pass

    def _dispatch(self, body):
        if not isinstance(body, dict):
            raise ValueError("invalid statistics action")
        with self.lock:
            if self._closed:
                raise OSError("statistics owner has stopped")
            action = body.get("action")
            if action == "status":
                return self.status()
            if action == "backup":
                return {"ok": True, "detail": "Verified statistics backup saved", "backup": self.backup()}
            if action == "reset":
                return self.reset(body.get("scope"), body.get("confirmation"))
            if action == "compact":
                self.maintain()
                self.checkpoint(force=True)
                return {"ok": True, "detail": "Database compacted; totals preserved"}
            raise ValueError("unknown statistics action")

    def _start_rpc(self):
        owner_id = uuid.uuid4().hex
        _write_message(self.directory / "owner.json", {"id": owner_id, "pid": os.getpid()})
        with _OWNERS_LOCK:
            _OWNERS[str(self.directory.resolve())] = self
        def worker():
            last_id = None
            while not self._rpc_stop.is_set():
                try:
                    request = _read_message(self.directory / "request.json")
                    request_id = request.get("id")
                    if request_id and request_id != last_id and request.get("owner") == owner_id:
                        last_id = request_id
                        try:
                            if float(request.get("expiresAt", 0)) < time.time():
                                raise TimeoutError("statistics request expired")
                            result = self._dispatch(request["body"])
                            response = {"id": request_id, "result": result}
                        except Exception as exc:
                            response = {"id": request_id, "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__, "errorType": type(exc).__name__}
                        _write_message(self.directory / "response.json", response)
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Missing/invalid mailbox cannot stop statistics processing.
                self._tick()
                self._rpc_stop.wait(0.025)
        self._rpc_thread = threading.Thread(target=worker, daemon=True, name="statistics-owner")
        self._rpc_thread.start()

    def close(self):
        if self._closed:
            return
        with _OWNERS_LOCK:
            key = str(self.directory.resolve())
            if _OWNERS.get(key) is self:
                _OWNERS.pop(key)
        self._rpc_stop.set()
        if self._rpc_thread:
            self._rpc_thread.join(timeout=2)
        with self.lock:
            if self._closed:
                return
            try:
                self.checkpoint(force=True)
            finally:
                self.db.close()
                self._closed = True
                (self.directory / "owner.json").unlink(missing_ok=True)
                self._guard.__exit__(None, None, None)


class DiskStatisticsStore(OwnerMixin, StatisticsStore):
    def _connect(self, timeout):
        recover_durable_journal(self.directory.parent.parent, self.connection, guarded=True)
        return super()._connect(timeout)


class MemoryStatisticsStore(OwnerMixin, StatisticsStore):
    storage_mode = "memory"

    def __init__(self, *args, **kwargs):
        self._checkpoint_at = 0.0
        self._checkpoint_mono = 0.0
        self._checkpointing = False
        self._checkpoint_retry_at = 0.0
        self._checkpoint_error = ""
        self._checkpoint_duration_ms = 0.0
        self._checkpoint_changes = -1
        super().__init__(*args, **kwargs)

    def _open_complete(self):
        self.journal_path = self.directory / "statistics.redo"
        self.journal_path.touch(mode=0o600, exist_ok=True)
        self.journal_path.chmod(0o600)
        self.checkpoint(force=True)
        self.db.journal = self.journal_path
        self.db.before_transaction = self._before_transaction

    def _before_transaction(self):
        if not self._checkpointing and self.journal_path.stat().st_size >= JOURNAL_LIMIT - RECORD_LIMIT:
            self.checkpoint(force=True)

    def _tick(self):
        now = time.monotonic()
        if now < self._checkpoint_retry_at or now - self._checkpoint_mono < self.settings["systemSqliteCheckpointS"]:
            return
        try:
            self.checkpoint()
        except Exception:
            # Keep the owner and mailbox responsive during a storage outage.
            # The journal remains intact; retry at a bounded rate.
            self._checkpoint_retry_at = now + min(5, self.settings["systemSqliteCheckpointS"])

    def _connect(self, timeout):
        root = self.directory.parent.parent
        recover_durable_journal(root, self.connection, guarded=True)
        db = sqlite3.connect(":memory:", timeout=timeout, check_same_thread=False, factory=DurableMemoryConnection)
        try:
            if self.path.exists():
                if self.path.stat().st_size > self.settings["systemDbMaxMb"] * MIB:
                    # Reclaim expired detail/free pages on disk before loading
                    # RAM. Lifetime totals and newest retained rows stay intact.
                    with closing(StatisticsStore(root, self.connection, self.settings, timeout=timeout)) as old:
                        for _ in range(16):
                            old.maintain(pressure=True)
                            used = old.db.execute("PRAGMA page_count").fetchone()[0] - old.db.execute("PRAGMA freelist_count").fetchone()[0]
                            if used * old.db.execute("PRAGMA page_size").fetchone()[0] < self.settings["systemDbMaxMb"] * MIB * .8:
                                break
                        old.db.execute("VACUUM")
                    if self.path.stat().st_size > self.settings["systemDbMaxMb"] * MIB:
                        raise ValueError("SQLite cannot fit the configured RAM ceiling")
                with closing(sqlite3.connect(self.path, timeout=timeout)) as source:
                    # Finish WAL migration before replacing any checkpoint file.
                    # This removes the crash window pairing a new DB with old WAL.
                    if source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                        raise sqlite3.OperationalError("statistics WAL is busy")
                    if source.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                        raise sqlite3.OperationalError("statistics WAL migration incomplete")
                    source.backup(db)
            return db
        except BaseException:
            db.close()
            raise

    def _snapshot(self, target):
        tmp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with closing(sqlite3.connect(tmp)) as destination:
                self.db.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("statistics checkpoint verification failed")
            tmp.chmod(0o600)
            with tmp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(tmp, target)
            _fsync_directory(target.parent)
        finally:
            tmp.unlink(missing_ok=True)

    def checkpoint(self, force=False):
        with self.lock:
            if self._closed:
                return False
            self.db.ensure_recovered()
            now = time.monotonic()
            journal_bytes = self.journal_path.stat().st_size
            if not force:
                if now - self._checkpoint_mono < self.settings["systemSqliteCheckpointS"] and journal_bytes < JOURNAL_LIMIT - RECORD_LIMIT:
                    return False
                if self._checkpoint_changes == self.db.total_changes and not journal_bytes:
                    self._checkpoint_mono = now
                    return False
            if self.db.in_transaction:
                raise RuntimeError("cannot checkpoint an uncommitted statistics transaction")
            stamp = time.time()
            self._checkpointing = True
            try:
                with self.db:
                    self._set("sqliteCheckpointAt", stamp)
                self._snapshot(self.path)
                with self.journal_path.open("wb") as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                self._checkpoint_at, self._checkpoint_mono = stamp, now
                self._checkpoint_changes = self.db.total_changes
                self._checkpoint_error = ""
                self._checkpoint_retry_at = 0.0
                self._checkpoint_duration_ms = (time.monotonic() - now) * 1000
                return True
            except Exception as exc:
                self._checkpoint_error = type(exc).__name__
                raise
            finally:
                self._checkpointing = False

    def backup(self, record=True):
        with self.lock:
            directory = self.directory / "backups"
            directory.mkdir(exist_ok=True, mode=0o700)
            target = directory / f"statistics-{time.time_ns()}.sqlite3"
            self._snapshot(target)
            for old in sorted(directory.glob("statistics-*.sqlite3"), reverse=True)[int(self.settings["systemBackupKeep"]):]:
                old.unlink()
            if record:
                with self.db:
                    self._set("lastBackupAt", time.time())
            return target.name

    def reset(self, scope, confirmation):
        if scope not in ("telemetry", "statistics") or confirmation != f"RESET {scope.upper()} {self.connection}":
            raise ValueError("exact connection confirmation required")
        with self.lock:
            # Single-owner lock covers backup + reset. Backup an idle connection:
            # backing up the same connection inside BEGIN would never finish.
            backup = self.backup(record=False)
            with self.db:
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
            self.checkpoint(force=True)
        return {"ok": True, "connection": self.connection, "scope": scope, "backup": backup,
                "detail": f"{scope.capitalize()} reset; verified backup saved. Evaluation history and open orders preserved."}

    def status(self):
        with self.lock:
            result = super().status()
            pages = self.db.execute("PRAGMA page_count").fetchone()[0]
            size = self.db.execute("PRAGMA page_size").fetchone()[0]
            return {**result, "storageMode": "memory", "memoryBytes": pages * size,
                    "checkpointAt": self._checkpoint_at, "journalBytes": self.journal_path.stat().st_size,
                    "checkpointError": self._checkpoint_error, "checkpointDurationMs": self._checkpoint_duration_ms,
                    "journalLimitBytes": JOURNAL_LIMIT, "memoryRestartRequired": not bool(self.settings["systemSqliteMemory"])}
