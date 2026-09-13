#!/usr/bin/env python3
"""Install an already reviewed CTS-G revision for CTS-GX X02 only.

Run as the server administrator with a full commit SHA present in /opt/cts-g.
Runtime state stays in /var/lib/cts-gx; X01 keeps its existing service and code.
"""
import json
import argparse
import pathlib
import re
import runpy
import shutil
import subprocess
import sys
import time
import urllib.request


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def profile_patch(current, defaults, keep_existing=False):
    patch = {k: current.get(k, v) if keep_existing else v for k, v in defaults.items()}
    # Explicit rollout requirements; preserve other current user settings.
    for key in ("controlMinTrades", "symbolCap", "maxOpen", "maxPerGroup", "setMaxActive", "entryPolicyMaxCandidates", "entryPolicyMinLiveSamples", "controlOrdersPerConfig", "controlOrdersOverall", "controlOrders"):
        patch[key] = defaults[key]
    return patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("revision")
    parser.add_argument("--keep-existing-settings", action="store_true")
    args = parser.parse_args()
    revision = args.revision
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
    for name in ('overlay-bingx-x02.json', 'open-bingx-x02.json', 'pending-bingx-x02.json', 'open-bingx-x02.json.controls'):
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
        # Retention is a systemd timer on the persistent checkout. Point its
        # oneshot at the same immutable release, otherwise a new error-log
        # policy would not be applied until the next full installer run.
        'cts-gx-retention.service': (f'{release}/deploy/retention.sh --once', release, False),
        'cts-gx-resources.service': (f'{python} -O {release}/deploy/dynamic-resources.py --name cts-gx --root /opt/cts-gx --apply', release, False),
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
    resource_unit = pathlib.Path('/etc/systemd/system/cts-gx-resources.service')
    resource_timer = pathlib.Path('/etc/systemd/system/cts-gx-resources.timer')
    for path in (resource_unit, resource_timer):
        if path.exists():
            shutil.copy2(path, backup/path.name)
    resource_unit.write_text(
        '[Unit]\nDescription=CTS-GX dynamic pulse resource policy\nAfter=local-fs.target\n\n'
        '[Service]\nType=oneshot\n'
        'EnvironmentFile=-/etc/cts-gx/cts-g.env\n'
        f'WorkingDirectory={release}\n'
        f'ExecStart={python} -O {release}/deploy/dynamic-resources.py --name cts-gx --root /opt/cts-gx --apply\n'
        'Nice=10\nPrivateTmp=true\nTimeoutStartSec=30\n'
        'StandardOutput=append:/var/log/cts-gx/resources.log\n'
        'StandardError=append:/var/log/cts-gx/resources.err.log\n'
        'SyslogIdentifier=cts-gx-resources\n'
    )
    resource_timer.write_text(
        '[Unit]\nDescription=Refresh CTS-GX pulse resource policy\n\n'
        '[Timer]\nOnBootSec=2min\nOnUnitActiveSec=60s\n'
        'RandomizedDelaySec=15s\nPersistent=true\n'
        'Unit=cts-gx-resources.service\n\n[Install]\nWantedBy=timers.target\n'
    )
    overlay = data/'overlay-bingx-x02.json'
    settings = json.loads(overlay.read_text()) if overlay.exists() else {}
    profile = runpy.run_path(str(release/'server/pulse/connection_profile.py'))
    settings.update(profile_patch(settings, profile['processing_profile'](), args.keep_existing_settings))
    temporary = overlay.with_suffix('.continuous.tmp')
    temporary.write_text(json.dumps(settings, indent=2) + '\n')
    temporary.replace(overlay)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'cts-gx-resources.timer')
    run('systemctl', '--no-block', 'restart', *units)
    current_live_pid = run('systemctl', 'show', 'cts-gx-pulse@bingx-x01.service', '-p', 'MainPID', '--value')
    if current_live_pid != old_live_pid:
        raise SystemExit('X01 process changed independently; inspect before proceeding')
    print(json.dumps(dict(revision=revision, release=str(release), backup=str(backup),
                          restarted=list(units), x01ProcessUnchanged=True, execution='VST_DEMO')))


if __name__ == '__main__':
    main()
