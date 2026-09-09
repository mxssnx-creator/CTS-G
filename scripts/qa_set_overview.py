"""Read-only loopback fixture for browser QA; never connects to an exchange."""
import argparse
import json
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "server/pulse"))
from contracts import INDICATION_KINDS
from set_engine import SetBook, SetState
from set_overview import build_overview


def fixture():
    book = SetBook()
    book.cost_pct = .1
    axes = []
    for pack in ("general", "indications"):
        for step in (3, 12):
            for family in ("base", "trail"):
                sid = f"{pack}:1m:sl0.6:st{step}:{family}"
                st = SetState(id=sid, pack=pack, tf="1m", sl_ratio=.6, trail_key="0.3:0.1" if family == "trail" else "",
                              trail_arm=.003, trail_give=.001, kind=family, step=step, tp_pct=step*.001, idx=len(book.by_idx))
                st.hist = [dict(t=1_800_000_000+i*60, symbol="TEST-USDT", side="LONG", pnl_pct=.004, hold_s=60, reason="tp") for i in range(15)]
                for kind in INDICATION_KINDS:
                    for strategy in ("core", "block", "dca", "axis"):
                        for i in range(15):
                            row = dict(t=1_800_000_000+i*60, symbol="TEST-USDT", side="LONG", pnl_pct=-.002 if strategy == "dca" else .003,
                                       pnl=-.003 if strategy == "dca" else .002, hold_s=120, reason="tp", ind_kind=kind,
                                       strategy=strategy, exchange_confirmed=True, tp_pct=st.tp_pct, execution_lane=f"{sid}:{kind}:{strategy}")
                            if strategy == "axis": row["axis_key"] = "last:5"
                            st.live.append(row)
                book.by_idx.append(st); book.sets[sid] = st
                if family == "base":
                    axes.append(dict(parentSetId=sid, axisKey="last:5", closedN=5, pf=1.2, qualified=True))
    for kind in INDICATION_KINDS:
        for step in (3,12):
            for i in range(15):
                row = dict(t=1_800_000_000+i*60, symbol="TEST-USDT", side="LONG", pnl_pct=.004, hold_s=60,
                           reason=f"ind:{kind}:tp", ind_kind=kind, tp_pct=step*.001, sl_ratio=.6, step=step, pack="indications")
                book.ind_hist.setdefault(kind, []).append(row)
                for strategy in ("block", "dca"):
                    book.strategy_hist.setdefault(strategy, []).append({**row, "strategy":strategy, "set_id":f"indications:sl0.6:st{step}"})
    overview = build_overview(book, axes)
    return dict(running=True, mode="QA_FIXTURE", connection="overall", connType="overall", exchange="Offline test data", unit="TEST",
                uptimeS=100, equity=10000, startEquity=10000, available=10000, usedMargin=0, unrealized=0, realizedPnl=0,
                sessionPnl=0, pnlPct=0, drawdownPct=0, wins=0, losses=0, winRate=0, openCount=0, maxOpen=250,
                symbols=["TEST-USDT"], symbolCount=1, open=[], closed=[], errors=0,
                coord={"axes":{"last":{"enabled":True,"max_window":5}}},
                sets={"overview":overview,"rows":[],"setCount":len(book.by_idx),"activeCount":4,
                      "progress":{"phase":"ready","pct":100,"ready":True,"detail":"Offline browser QA fixture"}})


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        data = json.loads(pathlib.Path(self.server.fixture_path).read_text())
        if self.path.startswith("/config.json"):
            data = {"ok":True,"cts":{},"overlay":{}}
        elif self.path.startswith("/connections.json"):
            data = {"selectedDefault":"overall","types":[{"type":"overall","label":"Offline test data","running":True}],"lanes":[],"slots":[]}
        elif self.path.startswith("/connection.json"):
            data = {"ok":True,"apiKeySet":False,"apiSecretSet":False}
        elif self.path.startswith("/user-presets.json"):
            data = {"presets":[]}
        elif self.path.startswith("/universe.json"):
            data = {"symbols":[]}
        elif self.path.startswith("/hist-calc.json"):
            data = {"status":"idle"}
        elif self.path.startswith("/system.json"):
            import time
            data = {"connection":"overall", "lanes":[{"connection":"bingx-x02","persistent":True,
                    "snapshotAt":time.time(),"cpuPct":18.5,"memoryMb":256,"dbKeys":350,"dbBytes":1048576,
                    "storageMode":"memory","memoryBytes":1048576,"journalBytes":8192,
                    "checkpointAt":time.time()-2,"checkpointDurationMs":3.5,
                    "requestsPerSec":2,"sessionRunningS":3600,"counters":{"recoveries":1,"crashes":0},
                    "calculationCache":{"hits":250,"misses":50,"cachedSets":350,"accountedBytes":1048576,
                                        "entryLimit":350,"trimTargetPct":80}}]}
        payload=json.dumps(data,allow_nan=False).encode()
        self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(payload)));self.end_headers();self.wfile.write(payload)

    def do_POST(self):
        self.send_error(405,"Browser fixture is read-only")

    def log_message(self,*args):
        pass


if __name__ == "__main__":
    parser=argparse.ArgumentParser();parser.add_argument("--output",required=True);parser.add_argument("--serve",type=int)
    args=parser.parse_args();pathlib.Path(args.output).write_text(json.dumps(fixture(),allow_nan=False))
    if args.serve:
        server=ThreadingHTTPServer(("127.0.0.1",args.serve),Handler);server.fixture_path=args.output;server.serve_forever()
