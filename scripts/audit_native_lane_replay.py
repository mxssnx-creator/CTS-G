#!/usr/bin/env python3
"""Compare selected replay entries with Pulse's real position manager on a stub venue.

No credentials, account connection or trading loop. OHLC bars are an assumed
open/adverse/favourable/close tick path, not measured exchange ticks. Admission is
supplied by trade_sim; this audit checks the selected parent's exit lifecycle.
Market fills occur at the stub tick, so barrier-fill prices may differ. Axis and
Block portfolio admission remain separate checks, not a claim of account parity.
"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "regression")]
import stage_eval as sg
import lanes


def run(report, data_dir):
    def no_network(*_args, **_kwargs):
        raise RuntimeError("native lane audit forbids network access")

    saved = socket.socket.connect, socket.create_connection
    socket.socket.connect = socket.create_connection = no_network
    try:
        book = sg.make_book()
        by_id = {s.id: s for s in book.by_idx}
        rows, loaded = [], {}
        for trade in report["trades"]:
            symbol = trade["symbol"]
            if symbol not in loaded:
                raw = json.loads((Path(data_dir) / (symbol + ".json")).read_text())
                loaded[symbol] = (raw, [[float(v) for v in r[1:6]] for r in raw])
            raw, bars = loaded[symbol]
            i = next(i for i, row in enumerate(raw) if row[0] / 1000 + 60 == trade["t_in"])
            end = next(i for i, row in enumerate(raw) if row[0] / 1000 + 60 == trade["t_out"])
            scaled = lanes._scaled(bars, i)
            st = by_id[trade["set"]]
            with contextlib.redirect_stdout(io.StringIO()):
                _, close, _ = lanes.live(book, st, scaled, i, trade["side"])
            record = {"symbol": symbol, "set": st.id, "entry_t": trade["t_in"],
                "replay_reason": trade["reason"], "replay_exit_bar": end,
                "replay_net_pct": trade["net_pct"], "native_close": close,
                "same_reason_and_bar": bool(close and close[:2] == (trade["reason"], end))}
            if close:
                closed = lanes.live.pulse.closed[-1]
                record.update(native_net_pct=100 * closed.net_pnl_pct,
                    native_fee_source=closed.fee_source,
                    replay_exit_price=bars[end][3] if trade["reason"] == "time" else None,
                    native_exit_price=close[2] * bars[i][3] / 100)
            rows.append(record)
        return {"scope": __doc__, "entries": len(rows),
            "same_reason_and_bar": sum(r["same_reason_and_bar"] for r in rows), "rows": rows}
    finally:
        socket.socket.connect, socket.create_connection = saved


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--patch", default="{}", help="same replay settings patch as the source report")
    args = parser.parse_args()
    os.environ["STAGE_OVERLAY_PATCH"] = args.patch
    result = run(json.loads(Path(args.report).read_text()), args.data)
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(f"Native parent exits: {result['same_reason_and_bar']}/{result['entries']} reason/bar matches; fill differences retained")
