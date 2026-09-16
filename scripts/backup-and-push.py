#!/usr/bin/env python3
"""Snapshot Live/VST overlays and push the git tip. Never flattens, never arms 8581."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP_DIR = ROOT / "backups" / "overlays"
KEEP = 14
SECRET_SUB = ("key", "secret", "token", "password", "passwd", "credential")
REMOTE_HOST = os.environ.get("CTS_BACKUP_HOST", "152.53.114.112")
SSH_KEY_CANDIDATES = [
    os.environ.get("CTS_BACKUP_SSH_KEY", ""),
    "/workspace/attachments/snet-ln-deb01.txt",
    str(Path.home() / ".ssh" / "snet-ln-deb01.txt"),
]


def run(cmd: list[str], *, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=True, **kw)


def sanitize(blob: dict) -> dict:
    out = {}
    for key, value in (blob or {}).items():
        low = str(key).lower()
        if any(part in low for part in SECRET_SUB):
            continue
        out[key] = value
    return out


def write_json(path: Path, blob: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(blob, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ssh_key() -> str | None:
    for raw in SSH_KEY_CANDIDATES:
        if raw and Path(raw).is_file():
            return raw
    return None


def fetch_remote_overlays() -> dict[str, dict]:
    key = ssh_key()
    if not key:
        return {}
    ssh = [
        "ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=12", f"root@{REMOTE_HOST}",
    ]
    script = r"""
python3 - <<'PY'
import json, glob, os
out = {}
for path in sorted(glob.glob("/var/lib/cts-ga/overlay-bingx-x0*.json")):
    try:
        blob = json.load(open(path))
    except Exception as exc:
        out[os.path.basename(path)] = {"error": str(exc)}
        continue
    out[os.path.basename(path)] = blob
status = {}
for path in sorted(glob.glob("/var/lib/cts-ga/stats-bingx-x0*.json")):
    try:
        d = json.load(open(path))
    except Exception:
        continue
    ht = d.get("histTest") if isinstance(d.get("histTest"), dict) else {}
    sets = d.get("sets") if isinstance(d.get("sets"), dict) else {}
    blk = d.get("block") if isinstance(d.get("block"), dict) else {}
    status[os.path.basename(path)] = {
        "mode": d.get("mode"),
        "running": d.get("running"),
        "halted": d.get("halted"),
        "openCount": d.get("openCount"),
        "equity": d.get("walletEquity") or d.get("equity"),
        "histTestEnabled": ht.get("enabled"),
        "histTestPhase": ht.get("phase"),
        "validatedCount": ht.get("validatedCount"),
        "processingCount": ht.get("processingCount") or sets.get("processingCount"),
        "activeCount": sets.get("activeCount"),
        "blockEnabled": blk.get("enabled"),
        "blockMaxStack": blk.get("maxStack"),
    }
print(json.dumps({"overlays": out, "status": status}))
PY
"""
    try:
        proc = run(ssh + [script], check=False, timeout=40)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {}


MAJORS = {
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT", "DOGE-USDT",
    "ADA-USDT", "BCH-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT", "DOT-USDT",
    "UNI-USDT", "ATOM-USDT", "NEAR-USDT", "APT-USDT", "ARB-USDT", "SUI-USDT",
    "INJ-USDT", "AAVE-USDT", "FIL-USDT", "OP-USDT", "TRX-USDT", "XLM-USDT",
    "ETC-USDT", "LDO-USDT", "HBAR-USDT", "TIA-USDT", "WLD-USDT", "JUP-USDT",
    "RENDER-USDT", "FET-USDT", "TAO-USDT", "SEI-USDT", "WIF-USDT", "1000PEPE-USDT",
    "STX-USDT", "IMX-USDT", "GRT-USDT", "ALGO-USDT", "VET-USDT", "EOS-USDT",
    "THETA-USDT", "AXS-USDT", "SAND-USDT", "MANA-USDT", "CRV-USDT", "MKR-USDT",
    "SNX-USDT", "COMP-USDT",
}


def overlay_corrupt(blob: dict) -> bool:
    if not isinstance(blob, dict) or blob.get("error"):
        return True
    if blob.get("blockEnabled") is False:
        return True
    names = [str(s).strip().upper() for s in (blob.get("symbols") or [])] if isinstance(blob.get("symbols"), list) else []
    wild = any(s in ("*", "ALL", "UNLIMITED") for s in names)
    try:
        cap = int(blob.get("symbolCap") or 0)
    except (TypeError, ValueError):
        cap = 0
    usdt = [s for s in names if s.endswith("-USDT")]
    majors_hit = [s for s in usdt if s in MAJORS]
    junk = [s for s in usdt if s not in MAJORS]
    if not wild and junk and (len(majors_hit) < 20 or len(junk) >= 3):
        return True
    if names and not wild and len(names) < 20 and not majors_hit:
        return True
    if cap and cap < 20 and not wild:
        return True
    return False


def restore_remote_overlays(names: list[str]) -> list[str]:
    """Replace a corrupted data-dir overlay with the workspace copy. Never restarts engines."""
    key = ssh_key()
    if not key:
        return [f"no-ssh {name}" for name in names]
    notes: list[str] = []
    for name in names:
        src = ROOT / "server" / "pulse" / name
        if not src.is_file():
            notes.append(f"missing {name}")
            continue
        tmp = f"/tmp/{name}.restore"
        dest = f"/var/lib/cts-ga/{name}"
        scp = run(
            [
                "scp", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=12", str(src), f"root@{REMOTE_HOST}:{tmp}",
            ],
            check=False,
            timeout=30,
        )
        if scp.returncode != 0:
            notes.append(f"scp-fail {name}")
            continue
        py = (
            "python3 - <<'PY'\n"
            "import json, os, shutil\n"
            f"src, dest = {tmp!r}, {dest!r}\n"
            "blob = json.load(open(src))\n"
            "assert isinstance(blob, dict) and blob.get('blockEnabled') is True\n"
            "tmp = dest + '.tmp'\n"
            "shutil.copy2(src, tmp)\n"
            "os.replace(tmp, dest)\n"
            "try:\n    os.remove(src)\nexcept OSError:\n    pass\n"
            "print('ok')\n"
            "PY"
        )
        ssh = [
            "ssh", "-i", key, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=12", f"root@{REMOTE_HOST}", py,
        ]
        proc = run(ssh, check=False, timeout=20)
        notes.append(f"ok {name}" if proc.returncode == 0 else f"ssh-fail {name}")
    return notes


def rotate(stamp_dir: Path) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dirs = sorted([p for p in BACKUP_DIR.iterdir() if p.is_dir() and p.name[:4].isdigit()], reverse=True)
    for old in dirs[KEEP:]:
        for child in old.glob("*"):
            child.unlink(missing_ok=True)
        old.rmdir()


def snapshot() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%MZ")
    dest = BACKUP_DIR / stamp
    latest = BACKUP_DIR / "latest"
    latest.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True, exist_ok=True)

    for name in ("overlay-bingx-x01.json", "overlay-bingx-x02.json"):
        src = ROOT / "server" / "pulse" / name
        if not src.is_file():
            continue
        blob = sanitize(json.loads(src.read_text(encoding="utf-8")))
        write_json(dest / f"repo-{name}", blob)
        write_json(latest / f"repo-{name}", blob)

    remote = fetch_remote_overlays()
    heal = [name for name, blob in (remote.get("overlays") or {}).items() if overlay_corrupt(blob if isinstance(blob, dict) else {})]
    if heal:
        notes = restore_remote_overlays(heal)
        write_json(dest / "heal.json", {"restored": notes})
        write_json(latest / "heal.json", {"restored": notes})
        remote = fetch_remote_overlays()
    else:
        leftover = latest / "heal.json"
        leftover.unlink(missing_ok=True)
    for name, blob in (remote.get("overlays") or {}).items():
        if not isinstance(blob, dict):
            continue
        clean = sanitize(blob)
        write_json(dest / f"remote-{name}", clean)
        write_json(latest / f"remote-{name}", clean)
    if remote.get("status"):
        write_json(dest / "status.json", remote["status"])
        write_json(latest / "status.json", remote["status"])
    rotate(dest)
    return dest


def git_push() -> str:
    os.chdir(ROOT)
    run([
        "git", "add",
        "backups/overlays/latest",
        "scripts/backup-and-push.py",
        ".gitignore",
        "server/pulse/pulse_http.py",
        "src/routes/settings.tsx",
        "scripts/test_settings_persistence.py",
    ], check=False)
    status = run(["git", "status", "--porcelain"], check=False)
    if status.stdout.strip():
        msg = "Backup Live/VST overlays and keep GitHub in sync."
        run(["git", "commit", "-m", msg], check=False)
    oauth = ""
    hosts = Path.home() / ".config/gh/hosts.yml"
    if hosts.is_file():
        for line in hosts.read_text(encoding="utf-8").splitlines():
            if "oauth_token:" in line:
                oauth = line.split(":", 1)[1].strip()
                break
    env = os.environ.copy()
    url = "origin"
    if oauth:
        env["GH_TOKEN"] = oauth
        url = f"https://x-access-token:{oauth}@github.com/mxssnx-creator/CTS-G.git"
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip() or "v0/runtime-bounds-live"
    push = run(["git", "push", url, f"HEAD:{branch}", "HEAD:main"], check=False, env=env)
    detail = (push.stderr or push.stdout or "").replace(oauth, "***") if oauth else (push.stderr or push.stdout or "")
    if push.returncode != 0:
        return f"push-failed {push.returncode} {detail[-400:]}"
    sha = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
    return f"pushed {sha} -> {branch} and main"


def main() -> int:
    dest = snapshot()
    result = git_push()
    print(f"backup {dest.relative_to(ROOT)}")
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
