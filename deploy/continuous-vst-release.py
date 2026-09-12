#!/usr/bin/env python3
"""Install an already reviewed CTS-G revision for CTS-GX X02 only.

Run as the server administrator with a full commit SHA present in /opt/cts-g.
Runtime state stays in /var/lib/cts-gx; X01 keeps its existing service and code.
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import urllib.request


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    revision = sys.argv[1]
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise SystemExit('A full immutable commit SHA is required')
    if run('git', '-C', '/opt/cts-g', 'rev-parse', revision) != revision:
        raise SystemExit('Revision is not available')
    stats = json.load(urllib.request.urlopen('http://127.0.0.1:3015/stats-bingx-x02.json', timeout=15))
    if stats.get('mode') != 'VST_DEMO':
        raise SystemExit('The target must already be the verified VST demo lane')
    old_live_pid = run('systemctl', 'show', 'cts-gx-pulse@bingx-x01.service', '-p', 'MainPID', '--value')
    release = pathlib.Path('/opt/cts-gx-releases') / revision[:12]
    backup = pathlib.Path('/var/backups/cts-gx') / ('continuous-' + str(int(time.time())))
    backup.mkdir(parents=True, mode=0o700)
    data = pathlib.Path('/var/lib/cts-gx')
    for name in ('overlay-bingx-x02.json', 'open-bingx-x02.json', 'pending-bingx-x02.json'):
        if (data/name).exists():
            shutil.copy2(data/name, backup/name)
    if not release.exists():
        run('git', '-C', '/opt/cts-g', 'worktree', 'add', '--detach', str(release), revision)
        (release/'node_modules').symlink_to('/opt/cts-gx/node_modules', target_is_directory=True)
        app_env = pathlib.Path('/opt/cts-gx/.grok/app-env.json')
        if app_env.exists():
            (release/'.grok').mkdir(exist_ok=True)
            shutil.copy2(app_env, release/'.grok/app-env.json')
    env = pathlib.Path('/etc/cts-gx/continuous-release.env')
    if env.exists():
        shutil.copy2(env, backup/env.name)
    env.write_text(f'CTS_G_ROOT={release}\nPULSE_DIR={release}/server/pulse\n')
    env.chmod(0o600)
    python = '/opt/cts-gx/.venv/bin/python'
    units = {
        'cts-gx-pulse@bingx-x02.service': (f'{python} -O {release}/server/pulse/pulse_trader.py', release/'server/pulse', True),
        'cts-gx-pulse-http.service': (f'{python} {release}/server/pulse/pulse_http.py', release/'server/pulse', False),
        'cts-gx-desk.service': (f'{release}/deploy/cts-g-desk.sh', release, False),
    }
    for unit, (command, cwd, demo) in units.items():
        drop = pathlib.Path('/etc/systemd/system') / (unit + '.d')
        drop.mkdir(exist_ok=True)
        target = drop/'90-continuous-release.conf'
        if target.exists():
            shutil.copy2(target, backup/(unit+'.conf'))
        target.write_text('[Service]\nEnvironmentFile=/etc/cts-gx/continuous-release.env\n'
                          f'WorkingDirectory={cwd}\nExecStart=\nExecStart={command}\n'
                          + ('Environment=CTS_VST_ONLY=1\n' if demo else ''))
    overlay = data/'overlay-bingx-x02.json'
    settings = json.loads(overlay.read_text()) if overlay.exists() else {}
    settings.update(dict(histLookbackBars=2880, baseEvalPosCount=30, setPfWindow=30, setMinSamples=30,
                         maxOpen=0, maxPerGroup=0, setMaxActive=0, entryPolicyMaxCandidates=0, symbolCap=20,
                         axisPrevEnabled=False, axisLastEnabled=False, axisContEnabled=False, axisPauseEnabled=False,
                         normalExecutionEnabled=True, stratTrailing=True, setUseHistoricGate=True, setStrictGate=True))
    for key in ('minPf', 'baseMinPf', 'mainMinPf', 'realMinPf', 'setMinPf', 'dcaMinPf', 'exitMinPf'):
        settings[key] = 1.05
    temporary = overlay.with_suffix('.continuous.tmp')
    temporary.write_text(json.dumps(settings, indent=2) + '\n')
    temporary.replace(overlay)
    run('systemctl', 'daemon-reload')
    run('systemctl', '--no-block', 'restart', *units)
    current_live_pid = run('systemctl', 'show', 'cts-gx-pulse@bingx-x01.service', '-p', 'MainPID', '--value')
    if current_live_pid != old_live_pid:
        raise SystemExit('X01 process changed independently; inspect before proceeding')
    print(json.dumps(dict(revision=revision, release=str(release), backup=str(backup),
                          restarted=list(units), x01ProcessUnchanged=True, execution='VST_DEMO')))


if __name__ == '__main__':
    main()
