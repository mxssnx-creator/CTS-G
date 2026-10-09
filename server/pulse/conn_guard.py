"""Pin each exchange connection to one BingX environment.

Live (x01) and virtual demo (x02, VST) can share one API key and secret. The environment is therefore chosen
only by the base URL. This module decides that URL from the connection name, refuses any stored or
environment setting that contradicts it, and derives the mode label from the URL.

A connection with no stored base URL gets its pinned URL, never the mainnet default.
"""
from __future__ import annotations

from typing import Tuple
from urllib.parse import urlparse

LIVE_MAINNET = "LIVE_MAINNET"
VST_DEMO = "VST_DEMO"
HOSTS = {"open-api.bingx.com": LIVE_MAINNET, "open-api-vst.bingx.com": VST_DEMO}
PINNED = {
    "bingx-x01": (LIVE_MAINNET, "https://open-api.bingx.com"),
    "bingx-x02": (VST_DEMO, "https://open-api-vst.bingx.com"),
}
TRUE_WORDS = ("1", "true", "yes")
FALSE_WORDS = ("0", "false", "no")


class ConnGuardError(RuntimeError):
    """Raised when a connection's stored or environment settings contradict its pinned environment."""


def _norm(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def mode_of(base: str) -> str:
    """The environment label for a base URL. Raises for an unknown host (never a silent default)."""
    host = urlparse(_norm(base)).hostname or ""
    if host not in HOSTS:
        raise ConnGuardError(f"unrecognised exchange host {host!r}")
    return HOSTS[host]


def resolve_base(conn: str, redis_base: str = "", is_testnet: str = "", env_base: str = "") -> Tuple[str, str]:
    """Return (base_url, mode) for a connection, or raise ConnGuardError on any contradiction."""
    key = (conn or "").replace("connection:", "").strip()
    if key not in PINNED:
        raise ConnGuardError(f"unknown connection {key!r}: it has no pinned environment")
    mode, base = PINNED[key]
    for label, value in (("stored base_url", redis_base), ("PULSE_BASE", env_base)):
        if value and _norm(value) != _norm(base):
            raise ConnGuardError(f"{key} is pinned to {mode} ({base}) but {label} is {value!r}")
    flag = (is_testnet or "").strip().lower()
    if mode == VST_DEMO and flag in FALSE_WORDS:
        raise ConnGuardError(f"{key} is the VST demo connection but is_testnet is {flag!r}")
    if mode == LIVE_MAINNET and flag in TRUE_WORDS:
        raise ConnGuardError(f"{key} is the live mainnet connection but is_testnet is {flag!r}")
    if mode_of(base) != mode:  # the table itself must agree with the host table
        raise ConnGuardError(f"pinned table inconsistent for {key}")
    return base, mode


def self_test():
    """Named checks for the regression suite: (name, passed, detail)."""
    out = []

    def expect_raise(name, fn):
        try:
            fn()
            out.append((name, False, "no error raised"))
        except ConnGuardError as exc:
            out.append((name, True, str(exc)[:90]))

    base, mode = resolve_base("bingx-x02")
    out.append(("guard.x02-no-config-is-vst", mode == VST_DEMO and "open-api-vst" in base, f"{mode} {base}"))
    base, mode = resolve_base("bingx-x02", "https://open-api-vst.bingx.com/", "1")
    out.append(("guard.x02-vst-config-accepted", mode == VST_DEMO, f"{mode} {base}"))
    base, mode = resolve_base("bingx-x01")
    out.append(("guard.x01-no-config-is-mainnet", mode == LIVE_MAINNET and base == "https://open-api.bingx.com", f"{mode} {base}"))
    expect_raise("guard.x02-mainnet-url-refused", lambda: resolve_base("bingx-x02", "https://open-api.bingx.com", "0"))
    expect_raise("guard.x02-testnet-false-refused", lambda: resolve_base("bingx-x02", "", "false"))
    expect_raise("guard.x01-vst-url-refused", lambda: resolve_base("bingx-x01", "https://open-api-vst.bingx.com", ""))
    expect_raise("guard.x01-testnet-true-refused", lambda: resolve_base("bingx-x01", "", "true"))
    expect_raise("guard.env-base-mismatch-refused", lambda: resolve_base("bingx-x02", "", "", "https://open-api.bingx.com"))
    expect_raise("guard.unknown-conn-refused", lambda: resolve_base("bingx-x09"))
    expect_raise("guard.unknown-host-refused", lambda: mode_of("https://example.com"))
    out.append(("guard.mode-of-vst", mode_of("https://open-api-vst.bingx.com") == VST_DEMO, ""))
    out.append(("guard.mode-of-mainnet", mode_of("https://open-api.bingx.com/") == LIVE_MAINNET, ""))
    return out


if __name__ == "__main__":
    for name, ok, detail in self_test():
        print(("ok " if ok else "FAIL ") + name, detail)
