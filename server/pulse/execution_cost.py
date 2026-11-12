"""Exchange fee schedules and fill-based accounting, with explicit estimates.

Rates are fractions; PositionCost and slippage configuration are percentages.
Paper uses a round-trip allowance. Actual fill prices already contain slippage:
only fees are deducted from their price return, never the allowance again.
"""
import math
from fee_policy import cost_policy, paper_cost_pct


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def fee_rate(value):
    value = number(value)
    return value if value is not None and 0 <= value <= .05 else None


class FeeSchedule:
    def __init__(self):
        self.account = {}
        self.next_query = 0.0
        self.error = ""
        self.retry_at = {}

    def refresh(self, api, symbols, now):
        """Read the account-wide commission once; the venue endpoint has no symbol parameter.

        One measured account response covers the requested symbols. Keep the
        existing per-symbol representation for held histories and persistence.
        https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-account/api-reference.md
        """
        symbols = list(dict.fromkeys(symbols))
        if now < self.next_query:
            return False
        wanted = set(symbols)
        self.account = {s: row for s, row in self.account.items() if s in wanted or now - row["t"] <= 7200}
        pending = [s for s in symbols if now - self.account.get(s, {}).get("t", float("-inf")) >= 3600]
        if not pending:
            return False
        self.next_query = now + 1
        try:
            account_wide = getattr(api, "commission_scope", "symbol") == "account"
            reply = api.get("/openApi/swap/v2/user/commissionRate", {} if account_wide else {"symbol": pending[0]})
            data = reply.get("data") or {}
            row = data.get("commission") or data
            taker, maker = fee_rate(row.get("takerCommissionRate")), fee_rate(row.get("makerCommissionRate"))
            if number(reply.get("code")) != 0 or taker is None or maker is None:
                self.error = f"commission rate unavailable ({reply.get('code')})"
                self.next_query = now + 60
                return False
            measured = {"taker": taker, "maker": maker, "t": now}
            for symbol in (symbols if account_wide else pending[:1]):
                self.account[symbol] = dict(measured)
            self.retry_at = {}
            self.error = ""
            return True
        except Exception:
            self.error = "commission rate request failed"
            self.next_query = now + 60
            return False

    def rates(self, symbol, contract=None, now=None):
        row = self.account.get(symbol)
        if row and (now is None or 0 <= now - row["t"] <= 7200):
            return row["maker"], row["taker"], "account-rate"
        maker = fee_rate(getattr(contract, "maker_fee", None))
        taker = fee_rate(getattr(contract, "taker_fee", None))
        return maker, taker, "contract-rate" if taker is not None else "configured-estimate"

    def paper_budget(self, policy, contracts, now):
        # One shared Set grid: include unknown selected symbols at the fallback
        # rate. A known cheap symbol must not mask an unmeasured expensive one.
        rates = [self.rates(s, c, now)[1] for s, c in contracts.items()]
        effective = [policy["takerFeeRate"] if r is None else r for r in rates]
        rate = max(effective) if effective else policy["takerFeeRate"]
        return paper_cost_pct(policy, rate, rate)

    def snapshot(self, now, contracts, budget_pct, slippage_pct):
        return {"paperCostPct": budget_pct, "slippageAllowancePct": slippage_pct,
                "executionSlippage": "included in actual fill prices; not deducted again",
                "error": self.error,
                "symbols": {s: dict(zip(("makerRate", "takerRate", "source"), self.rates(s, c, now)))
                            for s, c in contracts.items()}}


def fill_fee(data, qty, price, maker, taker, fallback_pct):
    """USDT fee debit and provenance. LIMIT alone never implies maker liquidity.

    Prefer a documented commission field in USDT. Unconverted fee assets are
    left as rate estimates. A reported zero fee is valid. The caller supplies
    venue-normalized signed debits (negative values represent rebates).
    """
    asset = str(data.get("commissionAsset") or data.get("feeAsset") or "USDT").upper()
    for key in ("commission", "fee"):
        value = number(data.get(key))
        if value is not None and asset == "USDT" and (value >= 0 or data.get("feeDebitNormalized") is True):
            return value, "reported-fee"
    is_maker = data.get("isMaker") in (True, "true", "TRUE")
    rate = maker if is_maker and maker is not None else taker
    if rate is not None:
        return qty * price * rate, "maker-rate" if is_maker and maker is not None else "taker-rate"
    # The fallback is a fee-only round-trip estimate. Fill prices already
    # incorporate execution slippage, so it must not include the paper allowance.
    return qty * price * fallback_pct / 200, "configured-estimate"


def execution_result(entry, exit_price, qty, side, entry_fee, exit_fee):
    notional = entry * qty
    gross = qty * (exit_price - entry) * (1 if side == "LONG" else -1)
    fees = entry_fee + exit_fee
    return {"gross": gross, "fees": fees, "net": gross - fees,
            "net_pct": (gross - fees) / notional if notional > 0 else 0.0}


def adverse_slippage(reference, fill, order_side):
    if reference <= 0:
        return 0.0
    return (fill / reference - 1) * (1 if order_side == "BUY" else -1)
