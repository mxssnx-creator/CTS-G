from copy import deepcopy
import unittest
from audit_six_hour_account import audit_report


class FinancialAuditTests(unittest.TestCase):
    def report(self):
        row = dict(id=1, qty=1, entry=100, exit=101, side=1, notional=100, gross=1,
                   fee_in=.09, fee_out=.09, fundingUsdt=-.01, net=.81)
        strategy = dict(n=1, pf=1.45, contributionUsdt=.81, hourContributionUsdt=.81,
                        closedNetUsdtToDate=.81, openMarkUsdt=0, openEntryFeesUsdt=0, openFundingUsdt=0)
        return dict(startEquity=1000, costPct=.18, symbols=["A"], account=dict(
            totals=dict(balanceEnd=1000.81, equityEnd=1000.81, pnl=.81, fees=.18, fundingUsdt=-.01,
                        maxDdPct=.1, closed=dict(n=1, pf=1.45)),
            openedPositions=[dict(id=1, fee_in=.09)], closedPositions=[row], openPositions=[],
            hourly=[dict(hour=1, equityEnd=1000.81, pnl=.81, feesUsdt=.18, fundingUsdt=-.01,
                         byStrategy=dict(normal=strategy), orders=dict(ordinaryRequests=2, batchRequests=1))],
            orderRateMinutes=[dict(bar=1, ordinaryRequests=2, ordinaryLimit=2, batchRequests=1, batchLimit=2)]))

    def test_conserved_cash_net_return_and_funding_pass(self):
        self.assertTrue(audit_report(self.report())["passed"])

    def test_cash_deducting_funding_twice_is_rejected(self):
        report = self.report()
        report["account"]["closedPositions"][0]["net"] -= .01
        result = audit_report(report)
        self.assertFalse(result["passed"])
        self.assertTrue(any("costs once" in row["check"] and not row["passed"] for row in result["checks"]))

    def test_classic_profit_factor_cannot_replace_return_pf(self):
        report = self.report()
        report["account"]["totals"]["closed"]["pf"] = 99
        self.assertFalse(audit_report(report)["passed"])

    def test_request_budget_overrun_and_contribution_gap_are_rejected(self):
        report = self.report()
        report["account"]["orderRateMinutes"][0]["ordinaryRequests"] = 3
        report["account"]["hourly"][0]["byStrategy"]["normal"]["openMarkUsdt"] = .5
        result = audit_report(report)
        self.assertFalse(result["passed"])
        self.assertEqual(sum(not row["passed"] for row in result["checks"]), 3)


if __name__ == "__main__":
    unittest.main()
