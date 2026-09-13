"""No-network execution benchmark; never represents exchange fill latency."""
import contextlib
import faulthandler
import io
import json
import pathlib
import statistics
import sys
import time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import test_all_valid_entries as harness
import overall_controls
from test_overall_controls import OverallTests

faulthandler.dump_traceback_later(30)
h=OverallTests()
try:
    p=h.pulse(25)
    timings=[];start=time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        for i in range(20):
            symbol=f'TEST{i}-USDT'
            p.contracts[symbol]=harness.pt.Contract(symbol,.001,.001,3,2,1.,100)
            p.px[symbol]=100
            for st in p.sets.by_idx:
                before=time.perf_counter()
                p.place(symbol,1,'trend',.9,selected_set=st)
                timings.append((time.perf_counter()-before)*1000)
        elapsed=time.perf_counter()-start
        for symbol in p.px:
            rows=[r for r in p.open.values() if r.symbol==symbol]
            if not rows:continue
            for _ in range(100):
                if not any(r.retired_control_ids for r in rows):break
                p._overall_cleanup_next=0;overall_controls.drain_retired(p,rows)
    assert len(p.open)==500
    assert len(p.api.orders)==40
    assert len({r.set_id for r in p.open.values()})==25
    result=dict(mode='offline-fake-exchange',symbols=20,configurationsPerSymbol=25,
        confirmedLogicalPositions=len(p.open),sharedProtectionPairs=len(p.api.orders)//2,
        elapsedS=round(elapsed,4),positionsPerS=round(len(p.open)/elapsed,2),
        perPositionMs=dict(p50=round(statistics.median(timings),3),p95=round(sorted(timings)[474],3),max=round(max(timings),3)),
        includesNetwork=False,includesVenueRateWait=False,
        note='Measures local scheduling/accounting with simulated fills. Actual API latency is reported separately in api.requestLatencyMs.')
    target=pathlib.Path(sys.argv[1]);target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
finally:
    faulthandler.cancel_dump_traceback_later()
    h.doCleanups()
