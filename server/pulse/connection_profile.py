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


# Shared evaluation minimum PF (Base, Main, Real, DCA, exits). Walk-forward over
# two consecutive 12h windows (scripts/min_pf_sweep.py, reports/min-pf-sweep.md)
# admitted 40% of entry candidates at 1.02 and ~15-20% at 1.20. The effect on PF
# is not stable (one window improved with the floor, the other got worse), so this
# is a selectivity setting, not a guarantee of profitable Sets. Slider range 1.02-1.35.
EVAL_MIN_PF = 1.20

# Order requests per second per lane. BingX allows 10 placements per second per IP
# (since 2025-10-16); Mainnet and VST can share one IP, so two lanes at 4.0 leave
# 20 % headroom. Same value as bingx_fast.ORDER_RPS and system-limits.json (a test
# keeps the three together).
ORDER_RPS = 4.0


def processing_profile():
    """One explicit profile for both lanes; no account/state/credential copy."""
    result = dict(histLookbackBars=2880, baseEvalPosCount=30, setPfWindow=30, setMinSamples=30, setDeactN=25, controlMinTrades=0,
                  maxOpen=100, maxPerGroup=0, setMaxActive=0, entryPolicyMaxCandidates=0, entryPolicyMinLiveSamples=0,
                  # Ranked 50-symbol book. maxOpen=100 is the effective-position
                  # cap (symbol × LONG/SHORT). Independent intern, Block, DCA
                  # and control lots on an occupied group are unlimited.
                  # The wildcard still selects the exchange universe before
                  # the ranked cap; it is not an unlimited live universe.
                  symbolsAll=True, symbolsDynamic=True, symbolCap=50,
                  maxRealSets=0, strategyLiveSetsCeiling=0,
                  strategyRealSetsSafetyCeiling=0,
                  axisPrevEnabled=False, axisLastEnabled=False,
                  axisContEnabled=False, axisPauseEnabled=False, normalExecutionEnabled=True,
                  stratTrailing=True, setUseHistoricGate=True, setStrictGate=True,
                  controlOrdersPerConfig=True, controlOrdersOverall=True, controlOrders=True)
    for key in ("minPf", "baseMinPf", "mainMinPf", "realMinPf", "setMinPf", "dcaMinPf", "exitMinPf"):
        result[key] = EVAL_MIN_PF
    # Order lane: the same ceiling on both lanes (BingX allows 10 placements/s per
    # IP; two lanes at 4.0/s leave 20 % headroom). Lanes keep any lower value they
    # were saved with unless this profile is applied.
    result["systemOrderRps"] = ORDER_RPS
    return result
