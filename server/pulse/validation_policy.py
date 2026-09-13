"""Optional chronological holdout admission; independent of live protections."""
CONTROL_MIN_TRADES = 0
CONTROL_MIN_PF = 1.0


def control_min_trades(value=CONTROL_MIN_TRADES):
    """Preserve explicit zero throughout settings, jobs and admission."""
    try:
        if isinstance(value, bool):
            return CONTROL_MIN_TRADES
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return CONTROL_MIN_TRADES
