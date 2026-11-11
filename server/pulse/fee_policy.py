"""Fee assumptions shared by paper/replay. Rates are fractions, cost is percent.

Fallbacks are operator assumptions, never a claim about a venue's current fees.
Slippage ratio 0.1 means 10% of the applicable fee, not 0.1% of notional.
"""
import math

DEFAULT_MAKER = .0005
DEFAULT_TAKER = .0008
DEFAULT_SLIPPAGE_RATIO = .1


def cost_policy(config=None):
    config = config or {}
    out = {}
    for key, default, maximum in (("makerFeeRate", DEFAULT_MAKER, .05),
                                  ("takerFeeRate", DEFAULT_TAKER, .05),
                                  ("slippageFeeRatio", DEFAULT_SLIPPAGE_RATIO, 10)):
        value = float(config.get(key, default))
        if not math.isfinite(value) or not 0 <= value <= maximum:
            raise ValueError(f"invalid {key}")
        out[key] = value
    return out


def paper_cost_pct(policy, entry_rate=None, exit_rate=None):
    entry = policy["takerFeeRate"] if entry_rate is None else entry_rate
    exit_ = policy["takerFeeRate"] if exit_rate is None else exit_rate
    return 100 * (entry + exit_) * (1 + policy["slippageFeeRatio"])


DEFAULT_COST_PCT = paper_cost_pct(cost_policy())
