#!/usr/bin/env python3
"""Read-only local desk bridge for a private native-session state directory.

No watchdog, system service actions, credentials or account mutations. Engine
control remains with the bounded session process that owns the order journal.
"""
import argparse
from pathlib import Path
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--port',type=int,default=3015)
    args=parser.parse_args()
    state=args.state.resolve(strict=True)
    if not state.is_dir():parser.error('state must be a native runtime directory')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'server/pulse'))
    import pulse_http as sidecar
    sidecar.DIR=str(state)
    sidecar.STOP_ALL_PATH=str(state/'STOP')
    sidecar.HTTP_TOKEN=''
    # Deliberately do not launch heal_loop: a local view must never start a
    # different live service or reload account credentials from shared Redis.
    print(f'Local read-only engine stats: http://127.0.0.1:{args.port}/stats.json?conn=bingx-x02',flush=True)
    sidecar.ThreadingHTTPServer(('127.0.0.1',args.port),sidecar.Handler).serve_forever()


if __name__=='__main__':main()
