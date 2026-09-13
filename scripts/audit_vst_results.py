#!/usr/bin/env python3
"""Read-only result audit of confirmed, complete X02 demo round trips."""
import argparse
import datetime as dt
import json
import pathlib
import sys
import time


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--data-dir',required=True)
    p.add_argument('--source-root',required=True)
    args=p.parse_args()
    sys.path.insert(0,str(pathlib.Path(args.source_root)/'server/pulse'))
    from position_cost import completed_roundtrips, last_n_cost_pf, row_notional
    root=pathlib.Path(args.data_dir)
    stats=json.loads((root/'stats-bingx-x02.json').read_text())
    if stats.get('mode')!='VST_DEMO':
        raise SystemExit('Expected the X02 demo statistics snapshot')
    rows=[];lines=0;invalid=0
    with (root/'trades-bingx-x02.jsonl').open() as f:
        for line in f:
            lines+=1
            try: row=json.loads(line)
            except ValueError:
                invalid+=1;continue
            if row.get('ours') is False or row.get('conn','bingx-x02')!='bingx-x02':continue
            if row.get('exchange_confirmed') is True:rows.append(row)
    closed=completed_roundtrips(rows)
    def metrics(rr):
        net=[float(r.get('pnl') or 0) for r in rr]
        gain=sum(max(0,v) for v in net);loss=-sum(min(0,v) for v in net)
        pf=last_n_cost_pf(rr,max(1,len(rr))) if rr else {}
        return dict(trades=len(rr),wins=sum(v>0 for v in net),losses=sum(v<0 for v in net),
                    netDemoUnits=sum(net),recordedFees=sum(float(r.get('fee_total') or 0) for r in rr),
                    classicPf=gain/loss if loss else None,ctsCostPf=pf.get('ratio'),
                    measuredCostSamples=pf.get('costSamples',0),notional=sum(row_notional(r) for r in rr),
                    firstClose=min((float(r['t']) for r in rr),default=None),
                    lastClose=max((float(r['t']) for r in rr),default=None))
    now=time.time();recent=[r for r in closed if float(r.get('t') or 0)>=now-7*86400]
    grouped={}
    for r in recent:
        key=(r.get('trail_set_id') or r.get('set_id') or 'unresolved',r.get('side'),r.get('strategy'),r.get('ind_kind'))
        grouped.setdefault(key,[]).append(r)
    payload=dict(capturedAt=dt.datetime.now(dt.timezone.utc).isoformat(),mode='VST_DEMO',
        source='retained trades-bingx-x02.jsonl; complete exchange-confirmed round trips only',
        journalRows=lines,malformedRows=invalid,confirmedLegs=len(rows),retained=metrics(closed),lastSevenDays=metrics(recent),
        perConfig=[dict(setId=k[0],side=k[1],strategy=k[2],indication=k[3],**metrics(v)) for k,v in sorted(grouped.items(),key=lambda i:repr(i[0]))],
        scope='Demo fills, not mainnet money. Retention can be shorter than seven days. Fee fallback is disclosed by measuredCostSamples; no estimated local close is counted.')
    print(json.dumps(payload,allow_nan=False))


if __name__=='__main__':main()
