"""Installation namespace: exchange ownership and Redis must not cross setups."""
import hashlib
import os
import re

NAME = os.environ.get("CTS_G_NAME", "cts-g")
if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", NAME):
    raise ValueError("Invalid CTS_G_NAME")


def redis_key(key: str) -> str:
    return os.environ.get("CTS_REDIS_PREFIX", NAME + ":") + key


def order_tag(slot: str) -> str:
    # Retain canonical legacy ownership so existing positions remain managed.
    lane = "x01" if slot == "bingx-x01" else "x02"
    if NAME == "cts-g":
        return "G" + lane
    return "G" + hashlib.sha256(NAME.encode()).hexdigest()[:6] + lane
