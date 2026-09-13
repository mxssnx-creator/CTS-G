#!/usr/bin/env python3
"""Prepare the shared release for stopped X01; never enable/start trading."""
import json
import pathlib
import re
import runpy
import shutil
import subprocess
import sys
import time


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def prepare(revision, root=pathlib.Path('/'), command=run):
    if not re.fullmatch(r'[a-f0-9]{40}',revision):
        raise ValueError('A full immutable revision is required')
    checkout = root/'opt/cts-g'
    data = root/'var/lib/cts-gx'
    stop = data/'STOP-bingx-x01'
    unit = 'cts-gx-pulse@bingx-x01.service'
    peer = 'cts-gx-pulse@bingx-x02.service'
    pid = lambda name: command('systemctl','show',name,'-p','MainPID','--value')
    if pid(unit) != '0' or not stop.is_file():
        raise RuntimeError('X01 must already be stopped with its STOP marker present')
    stop_bytes, stop_mtime = stop.read_bytes(), stop.stat().st_mtime_ns
    peer_pid = pid(peer)
    if command('git','-C',str(checkout),'rev-parse',revision) != revision:
        raise RuntimeError('Revision unavailable')
    release = root/'opt/cts-gx-releases'/revision[:12]
    if not release.exists():
        command('git','-C',str(checkout),'worktree','add','--detach',str(release),revision)
    profile = runpy.run_path(str(release/'server/pulse/connection_profile.py'))
    profile['connection_endpoint']('bingx-x01')
    overlay = data/'overlay-bingx-x01.json'
    settings = json.loads(overlay.read_text()) if overlay.exists() else {}
    settings.update(profile['processing_profile']())
    backup = root/'var/backups/cts-gx'/('mainnet-prepared-'+str(time.time_ns()))
    backup.mkdir(parents=True,mode=0o700)
    shutil.copy2(stop,backup/stop.name)
    if overlay.exists():shutil.copy2(overlay,backup/overlay.name)
    drop = root/'etc/systemd/system'/(unit+'.d')/'95-shared-mainnet.conf'
    drop.parent.mkdir(parents=True,exist_ok=True)
    if drop.exists():shutil.copy2(drop,backup/drop.name)
    env = root/'etc/cts-gx/mainnet-shared-release.env'
    env.parent.mkdir(parents=True,exist_ok=True)
    if env.exists():shutil.copy2(env,backup/env.name)
    env.write_text(f'CTS_G_ROOT={release}\nPULSE_DIR={release}/server/pulse\n'
                   'PULSE_CONN=bingx-x01\nCTS_VST_ONLY=0\n')
    env.chmod(0o600)
    python = root/'opt/cts-gx/.venv/bin/python'
    # A final EnvironmentFile overrides older installation EnvironmentFiles;
    # plain Environment= would be overridden by those earlier files.
    drop.write_text('[Service]\n'+f'EnvironmentFile={env}\n'
                    +f'WorkingDirectory={release}/server/pulse\nExecStart=\n'
                    +f'ExecStart={python} -O {release}/server/pulse/pulse_trader.py\n')
    temporary = overlay.with_suffix('.prepared.tmp')
    temporary.write_text(json.dumps(settings,indent=2)+'\n')
    temporary.replace(overlay)
    command('systemctl','daemon-reload')
    if pid(unit) != '0' or not stop.is_file() or stop.read_bytes()!=stop_bytes or stop.stat().st_mtime_ns!=stop_mtime:
        raise RuntimeError('Mainnet stopped-state changed independently; inspect before continuing')
    if pid(peer) != peer_pid:
        raise RuntimeError('VST process changed independently during mainnet preparation')
    return dict(revision=revision,release=str(release),backup=str(backup),connection='bingx-x01',
                profile=profile['processing_profile'](),mainnetStarted=False,
                stopPreserved=True,vstProcessUnchanged=True)


if __name__ == '__main__':
    print(json.dumps(prepare(sys.argv[1])))
