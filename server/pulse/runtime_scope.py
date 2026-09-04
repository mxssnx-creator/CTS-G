"""Installation namespace: exchange ownership and Redis must not cross setups."""
import hashlib
import os
import re

NAME = os.environ.get("CTS_G_NAME", "cts-g")
if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", NAME):
    raise ValueError("Invalid CTS_G_NAME")


def redis_key(key: str) -> str:
    # Existing canonical services have unscoped keys. A new installer explicitly
    # sets the namespace after copying those keys. Never orphan active positions
    # merely because an update added this module before that migration.
    default = "" if NAME == "cts-g" else NAME + ":"
    prefix = os.environ.get("CTS_REDIS_PREFIX", default)
    if NAME != "cts-g" and prefix != NAME + ":":
        raise ValueError("Named installation requires its own Redis namespace")
    return prefix + key


def order_tag(slot: str) -> str:
    # Retain canonical legacy ownership so existing positions remain managed.
    if slot not in ("bingx-x01", "bingx-x02"):
        raise ValueError("Unsupported connection slot")
    lane = "x01" if slot == "bingx-x01" else "x02"
    if NAME == "cts-g":
        return "G" + lane
    return "G" + hashlib.sha256(NAME.encode()).hexdigest()[:6] + lane
