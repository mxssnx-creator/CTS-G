"""Shared processing structure with explicit, isolated exchange connections."""
from urllib.parse import urlparse

ENDPOINTS = {
    "bingx-x01": "https://open-api.bingx.com",
    "bingx-x02": "https://open-api-vst.bingx.com",
}


def connection_endpoint(connection, configured_url="", is_testnet="", *, vst_only=False):
    if connection not in ENDPOINTS:
        raise ValueError("Unsupported exchange connection")
    if vst_only and connection != "bingx-x02":
        raise ValueError("VST-only deployment requires X02")
    if connection == "bingx-x01" and str(is_testnet).lower().strip() in ("1", "true", "yes"):
        raise ValueError("Mainnet connection cannot use testnet credentials configuration")
    base = str(configured_url or ENDPOINTS[connection]).strip()
    parsed = urlparse(base)
    expected = urlparse(ENDPOINTS[connection])
    if (parsed.scheme != "https" or parsed.hostname != expected.hostname
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Exchange endpoint does not match the selected connection")
    return ENDPOINTS[connection]


def processing_profile():
    """One explicit profile for both lanes; no account/state/credential copy."""
    result = dict(histLookbackBars=2880, baseEvalPosCount=30, setPfWindow=30, setMinSamples=30, setDeactN=25,
                  maxOpen=0, maxPerGroup=0, setMaxActive=0, entryPolicyMaxCandidates=0,
                  symbolCap=20, axisPrevEnabled=False, axisLastEnabled=False,
                  axisContEnabled=False, axisPauseEnabled=False, normalExecutionEnabled=True,
                  stratTrailing=True, setUseHistoricGate=True, setStrictGate=True,
                  controlOrdersPerConfig=True)
    for key in ("minPf", "baseMinPf", "mainMinPf", "realMinPf", "setMinPf", "dcaMinPf", "exitMinPf"):
        result[key] = 1.02
    return result
