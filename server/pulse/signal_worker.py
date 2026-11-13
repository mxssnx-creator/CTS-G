"""Worker process of the signal pool (signal_pool.py): reads pickled tasks from stdin, answers pickled rows on stdout.

A task is (bars_slice, relative_indices, packs, ind_settings, base_ts_of_slice_start, bar_offset); the answer is the list of `set_engine.bar_signal` rows, one per index.
`bar_signal` reads only the closed bars inside its own window ending at the index, so a slice that starts a full signal window before the first index gives exactly the rows
the engine would compute in its own process. Nothing here touches the network, the files of the engine or any state; stdout carries only the framed answers."""
import os
import pickle
import struct
import sys

_OUT = os.fdopen(os.dup(1), "wb", buffering=0)
os.dup2(2, 1)                                   # an import that prints must not corrupt the framed answers


def _read(n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from set_engine import bar_signal, BAR_S
    while True:
        head = _read(8)
        if head is None:
            return
        body = _read(struct.unpack("<Q", head)[0])
        if body is None:
            return
        bars, rel, packs, settings, base_ts, off = pickle.loads(body)
        rows = [bar_signal(bars, i, packs, settings, base_ts + (off + i) * BAR_S) for i in rel]
        blob = pickle.dumps(rows, 5)
        _OUT.write(struct.pack("<Q", len(blob)) + blob)


if __name__ == "__main__":
    main()
