#!/usr/bin/env python3
"""Calculate and apply a safe, host-aware pulse resource policy.

The pulse catalog is intentionally allowed to use the memory the host can
spare, but the policy always keeps a system/Redis reserve and never lowers a
live cgroup maximum below the process' current working set.  CPU has no
quota; the pulse lane receives the highest systemd weight so idle CPU is
available to it without starving the kernel or the sidecars by construction.

This helper is run once before a release and periodically by the scoped
resources timer.  It only manages the named installation's pulse template and
currently active pulse instances.  It never starts, stops, enables, or
restarts a trading service.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

MIB = 1024 * 1024
MIN_RESERVE_MB = 2048.0
MAX_RESERVE_MB = 4096.0
MIN_GROWTH_MB = 768.0
MAX_GROWTH_MB = 4096.0
MIN_PULSE_MAX_MB = 3072.0
DEFAULT_PULSE_MAX_MB = 6144.0
ROUND_MB = 64.0
SAFETY_MARGIN_MB = 256.0
CPU_WEIGHT_MAX = 10000


def read_meminfo(path: os.PathLike[str] | str = "/proc/meminfo") -> Tuple[float, float]:
    """Return ``(MemTotal, MemAvailable)`` in MiB, or zeros if unavailable."""
    total = 0.0
    available = 0.0
    try:
        with open(path, encoding="ascii") as handle:
            for line in handle:
                key, _, raw = line.partition(":")
                if key not in ("MemTotal", "MemAvailable"):
                    continue
                fields = raw.split()
                if not fields:
                    continue
                value = float(fields[0])
                # Linux reports these fields in KiB.  Accept bytes in tests or
                # on alternate proc implementations when the unit is explicit.
                unit = fields[1].lower() if len(fields) > 1 else "kb"
                divisor = 1024.0 if unit in ("kb", "kib") else 1.0 if unit in ("mb", "mib") else MIB
                if key == "MemTotal":
                    total = max(0.0, value / divisor)
                else:
                    available = max(0.0, value / divisor)
    except (OSError, TypeError, ValueError):
        return 0.0, 0.0
    return total, available


def _round_mb(value: float) -> int:
    return max(1, int(math.ceil(max(0.0, float(value)) / ROUND_MB) * ROUND_MB))


def _reserve_mb(total_mb: float) -> float:
    if total_mb <= 0:
        return MIN_RESERVE_MB
    return max(MIN_RESERVE_MB, min(MAX_RESERVE_MB, total_mb * 0.18))


def _host_ceiling_mb(total_mb: float, reserve_mb: float) -> float:
    if total_mb <= 0:
        return 0.0
    return max(MIN_PULSE_MAX_MB, total_mb - reserve_mb)


def _growth_mb(available_mb: float) -> float:
    if available_mb <= 0:
        return 1536.0
    # MemAvailable is the amount the kernel can reclaim without swapping.
    # Give the lane only a bounded fraction of it; the remainder stays
    # available for Redis, the desk, the OS and transient kernel reclaim.
    return max(MIN_GROWTH_MB, min(MAX_GROWTH_MB, available_mb * 0.35))


def compute_policy(
    total_mb: float,
    available_mb: float,
    current_by_unit: Mapping[str, float],
) -> Dict[str, Any]:
    """Return a deterministic policy from host memory and live cgroup usage."""
    current = {
        str(unit): max(0.0, float(value or 0.0))
        for unit, value in current_by_unit.items()
        if str(unit)
    }
    active = len(current)
    reserve = _reserve_mb(float(total_mb or 0.0))
    ceiling = _host_ceiling_mb(float(total_mb or 0.0), reserve)
    growth = _growth_mb(float(available_mb or 0.0))

    if active and any(value > 0 for value in current.values()):
        current_sum = sum(current.values())
        target_pool = current_sum + growth
        if ceiling > 0:
            target_pool = min(target_pool, ceiling)
        target_pool = max(target_pool, current_sum + SAFETY_MARGIN_MB * active)
        target_mb = target_pool / active
    else:
        # Before the first process starts there is no current cgroup sample.
        # Keep enough room for the catalog's initial load; the first timer pass
        # after startup will recalculate from the real working set.
        base = DEFAULT_PULSE_MAX_MB
        if ceiling > 0:
            base = min(base, ceiling)
        target_mb = max(MIN_PULSE_MAX_MB, base, growth)

    live_floor = max(current.values(), default=0.0) + SAFETY_MARGIN_MB
    if live_floor > target_mb:
        target_mb = live_floor
    if ceiling > 0 and target_mb < live_floor:
        # A host measurement can be stale while the cgroup is already full.
        # Keeping the live floor is safer than forcing an immediate OOM kill.
        target_mb = live_floor

    memory_max_mb = _round_mb(target_mb)
    memory_high_mb = _round_mb(max(memory_max_mb * 0.90, live_floor + 128.0))
    if memory_high_mb >= memory_max_mb:
        memory_high_mb = max(1, memory_max_mb - int(ROUND_MB))

    return {
        "memoryHighMb": memory_high_mb,
        "memoryMaxMb": memory_max_mb,
        "memoryReserveMb": round(reserve, 1),
        "memoryGrowthMb": round(growth, 1),
        "hostTotalMb": round(max(0.0, float(total_mb or 0.0)), 1),
        "hostAvailableMb": round(max(0.0, float(available_mb or 0.0)), 1),
        "activePulseCount": active,
        "currentByUnitMb": {unit: round(value, 1) for unit, value in current.items()},
        "cpuWeight": CPU_WEIGHT_MAX,
        "cpuQuota": "infinity",
        "memorySwapMax": 0,
        "calculatedAt": int(time.time()),
    }


def dropin_text(policy: Mapping[str, Any]) -> str:
    """Serialize only the resource properties managed by this helper."""
    high = _round_mb(float(policy["memoryHighMb"]))
    maximum = _round_mb(float(policy["memoryMaxMb"]))
    if high >= maximum:
        high = max(1, maximum - int(ROUND_MB))
    return (
        "# Managed by deploy/dynamic-resources.py; do not edit by hand.\n"
        "[Service]\n"
        f"MemoryHigh={high}M\n"
        f"MemoryMax={maximum}M\n"
        "MemorySwapMax=0\n"
        f"CPUWeight={CPU_WEIGHT_MAX}\n"
        "CPUQuota=infinity\n"
    )


def _completed(
    args: Sequence[str],
    runner=subprocess.run,
    *,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    return runner(args, text=True, capture_output=True, check=check)


def active_pulse_units(name: str, runner=subprocess.run) -> Sequence[str]:
    """List active instance units belonging to one installation."""
    prefix = f"{name}-pulse@"
    result = _completed(
        ["systemctl", "list-units", "--type=service", "--state=active", "--no-legend", "--plain"],
        runner,
    )
    if result.returncode != 0:
        return []
    units = []
    for line in (result.stdout or "").splitlines():
        unit = line.split(None, 1)[0] if line.split(None, 1) else ""
        if unit.startswith(prefix) and unit.endswith(".service"):
            units.append(unit)
    return tuple(dict.fromkeys(units))


def unit_memory_current(unit: str, runner=subprocess.run) -> float:
    result = _completed(
        ["systemctl", "show", unit, "-p", "MemoryCurrent", "--value", "--no-pager"],
        runner,
    )
    if result.returncode != 0:
        return 0.0
    raw = (result.stdout or "").strip()
    try:
        return max(0.0, float(raw) / MIB)
    except (TypeError, ValueError):
        return 0.0


def apply_policy(
    name: str,
    *,
    root: os.PathLike[str] | str = "/opt/cts-g",
    systemd_dir: os.PathLike[str] | str = "/etc/systemd/system",
    runner=subprocess.run,
    apply_runtime: bool = True,
) -> Dict[str, Any]:
    """Calculate, persist, and optionally apply the resource policy."""
    units = active_pulse_units(name, runner)
    current = {unit: unit_memory_current(unit, runner) for unit in units}
    total, available = read_meminfo()
    policy = compute_policy(total, available, current)
    policy["installation"] = name
    policy["root"] = str(root)
    policy["activeUnits"] = list(units)

    drop_dir = pathlib.Path(systemd_dir) / f"{name}-pulse@.service.d"
    drop_dir.mkdir(parents=True, exist_ok=True)
    # Keep this after release/mainnet ExecStart drop-ins. Older release
    # scripts also placed their resource values in a 90-* drop-in; a dynamic
    # policy must win over those stale values without touching the ExecStart.
    target = drop_dir / "99-dynamic-resources.conf"
    text = dropin_text(policy)
    changed = not target.exists() or target.read_text(encoding="utf-8") != text
    if changed:
        temporary = target.with_suffix(".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
        if apply_runtime:
            _completed(["systemctl", "daemon-reload"], runner, check=True)

    runtime_errors = []
    if apply_runtime:
        max_bytes = str(int(policy["memoryMaxMb"] * MIB))
        high_bytes = str(int(policy["memoryHighMb"] * MIB))
        for unit in units:
            try:
                _completed(
                    [
                        "systemctl",
                        "set-property",
                        "--runtime",
                        unit,
                        f"MemoryHigh={high_bytes}",
                        f"MemoryMax={max_bytes}",
                        "MemorySwapMax=0",
                        f"CPUWeight={CPU_WEIGHT_MAX}",
                    ],
                    runner,
                    check=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                runtime_errors.append(f"{unit}: {type(exc).__name__}")

    root_path = pathlib.Path(root)
    # Keep diagnostics beside durable runtime state, not inside the Git
    # checkout.  /opt/<name> maps to /var/lib/<name>; custom install prefixes
    # retain the same prefix.
    metadata = root_path.parent.parent / "var/lib" / name / "resource-policy.json"
    try:
        metadata.parent.mkdir(parents=True, exist_ok=True)
        temporary = metadata.with_suffix(".tmp")
        temporary.write_text(json.dumps({**policy, "dropinChanged": changed, "runtimeErrors": runtime_errors}, indent=2) + "\n", encoding="utf-8")
        temporary.replace(metadata)
    except OSError:
        # The systemd policy is the authority; metadata is diagnostic only.
        pass
    if runtime_errors:
        raise RuntimeError("; ".join(runtime_errors))
    return {**policy, "dropinChanged": changed, "runtimeErrors": runtime_errors}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--root", default="/opt/cts-g")
    parser.add_argument("--systemd-dir", default="/etc/systemd/system")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", args.name):
        parser.error("invalid installation name")
    try:
        result = apply_policy(
            args.name,
            root=args.root,
            systemd_dir=args.systemd_dir,
            apply_runtime=args.apply,
        )
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"dynamic resource policy failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
