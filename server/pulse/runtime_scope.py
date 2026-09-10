"""Installation namespace: exchange ownership and Redis must not cross setups."""
import hashlib
import os
import re

NAME = os.environ.get("CTS_G_NAME", "cts-g")
if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", NAME):
    raise ValueError("Invalid CTS_G_NAME")


def normalize_system_id(value: str = "") -> str:
    system = str(value or NAME).strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", system):
        raise ValueError("Invalid system id")
    return system


def normalize_connection_id(value: str) -> str:
    connection = str(value or "").strip().lower().removeprefix("connection:")
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,39}", connection):
        raise ValueError("Invalid connection id")
    return connection


def system_id() -> str:
    return normalize_system_id()


def tracking_scope(connection: str, system: str = "") -> str:
    return f"{normalize_system_id(system)}:{normalize_connection_id(connection)}"


def scope_metadata(connection: str, system: str = "") -> dict[str, str]:
    connection_id = normalize_connection_id(connection)
    return {
        "systemId": normalize_system_id(system),
        "connection": connection_id,
        "trackingScope": tracking_scope(connection_id, system),
        "trackPrefix": order_tag(connection_id),
    }


def row_scope_matches(row: object, connection: str, *, allow_legacy_client: bool = True) -> bool:
    """Prove that a persisted row belongs to one exact system/connection lane."""
    if not isinstance(row, dict):
        return False
    expected_system = normalize_system_id()
    expected_connection = normalize_connection_id(connection)
    expected_scope = tracking_scope(expected_connection, expected_system)
    actual_scope = str(row.get("tracking_scope") or row.get("trackingScope") or "").strip().lower()
    row_system = str(row.get("system_id") or row.get("systemId") or "").strip().lower()
    row_connection = str(row.get("connection") or row.get("conn") or "").strip().lower().removeprefix("connection:")
    if actual_scope:
        if actual_scope != expected_scope:
            return False
        if row_system and row_system != expected_system:
            return False
        if row_connection and row_connection != expected_connection:
            return False
        return True
    if row_system or row_connection:
        return row_system == expected_system and row_connection == expected_connection
    if allow_legacy_client:
        client_id = str(row.get("client_id") or row.get("clientId") or "").strip().lower()
        return client_id.startswith(order_tag(expected_connection).lower())
    return False


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
