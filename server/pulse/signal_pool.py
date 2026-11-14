"""A pool of worker processes that computes per-bar pack signals in parallel (opt-in: overlay `replayWorkers`, default 0 = off).

Why: 99 % of a Set rebuild is `set_engine.bar_signal` on every closed bar, a pure function of the bars inside its own window, so the bars of one symbol can be computed by
several processes and merged in index order with exactly the same result. The workers are plain subprocesses (not multiprocessing: that would re-import the engine's main
module), started on first use, restarted when one dies, and ended with the process. Any failure returns None and the caller computes in its own process as before."""
from __future__ import annotations

import os
import pickle
import queue
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_INDICES = 96            # fewer bars than this are computed in the caller's own process (the pool's start and transfer would cost more)
MAX_WORKERS = 6


class _Worker:
    def __init__(self) -> None:
        self.proc = subprocess.Popen([sys.executable, "-B", os.path.join(HERE, "signal_worker.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, cwd=HERE)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, task: Any) -> List[Any]:
        blob = pickle.dumps(task, 5)
        self.proc.stdin.write(struct.pack("<Q", len(blob)) + blob)
        self.proc.stdin.flush()
        head = self._read(8)
        return pickle.loads(self._read(struct.unpack("<Q", head)[0]))

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                raise EOFError("signal worker ended")
            buf += chunk
        return bytes(buf)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


_LOCK = threading.Lock()
_IDLE: "queue.Queue[_Worker]" = queue.Queue()
_ALL: List[_Worker] = []


def _take(n_wanted: int) -> _Worker:
    try:
        w = _IDLE.get_nowait()
        if w.alive():
            return w
        with _LOCK:
            if w in _ALL:
                _ALL.remove(w)
    except queue.Empty:
        pass
    with _LOCK:
        if len([x for x in _ALL if x.alive()]) < n_wanted:
            w = _Worker()
            _ALL.append(w)
            return w
    return _IDLE.get(timeout=600)


def shutdown() -> None:
    with _LOCK:
        for w in _ALL:
            w.close()
        _ALL.clear()
    while not _IDLE.empty():
        try:
            _IDLE.get_nowait()
        except queue.Empty:
            break


def compute(bars: Sequence[Sequence[float]], indices: Sequence[int], packs: Sequence[str], settings: Dict[str, Any], base_ts: float, window: int,
            workers: int, on_chunk: Optional[Callable[[], None]] = None) -> Optional[Dict[int, Dict[str, Any]]]:
    """{bar index: row} for every index, or None when the pool is off, too small or failed (the caller then computes them itself)."""
    workers = max(0, min(int(workers or 0), MAX_WORKERS, os.cpu_count() or 1))
    idx = sorted(indices)
    if workers < 1 or len(idx) < MIN_INDICES:
        return None
    size = max(24, min(240, -(-len(idx) // (workers * 3))))
    chunks = [idx[k:k + size] for k in range(0, len(idx), size)]

    def one(chunk: List[int]):
        lo = max(0, chunk[0] + 1 - window)
        task = ([list(r) for r in bars[lo:chunk[-1] + 1]], [i - lo for i in chunk], list(packs), settings, base_ts, lo)
        w = _take(workers)
        try:
            rows = w.run(task)
        except Exception:
            with _LOCK:
                if w in _ALL:
                    _ALL.remove(w)
            w.close()
            raise
        _IDLE.put(w)
        return chunk, rows
    out: Dict[int, Dict[str, Any]] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(one, c) for c in chunks]
            for f in as_completed(futures):
                chunk, rows = f.result()
                for i, row in zip(chunk, rows):
                    out[i] = row
                if on_chunk:
                    on_chunk()
    except Exception:
        return None
    return out if len(out) == len(idx) else None
