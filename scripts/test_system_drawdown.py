import pathlib, sys, unittest
from types import SimpleNamespace
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'server/pulse'))
from pulse_trader import Pulse

class SystemDrawdown(unittest.TestCase):
    def measure(self,pnls,capital=100,upnl=0):
        p=Pulse.__new__(Pulse);p.start_eq=capital;p.open={}
        p.strategy_closes=lambda:[SimpleNamespace(t=i,pnl=x,qty=1,entry=100) for i,x in enumerate(pnls)]
        p.system_open_upnl=lambda:upnl
        return p.system_activity()

    def test_loss_from_start_is_not_zero(self):
        self.assertEqual(self.measure([-1,-2])['drawdownPct'],3)
    def test_small_first_profit_is_not_capital(self):
        self.assertAlmostEqual(self.measure([.01,-1])['drawdownPct'],1,places=3)
    def test_each_drawdown_uses_its_own_peak(self):
        self.assertEqual(self.measure([100,-50,1000])['drawdownPct'],25)
    def test_current_unrealized_loss_is_included(self):
        self.assertEqual(self.measure([],upnl=-5)['drawdownPct'],5)
    def test_missing_capital_is_explicitly_unavailable(self):
        self.assertFalse(self.measure([-5],capital=0)['drawdownAvailable'])

    def test_foreign_trade_and_mark_do_not_enter_system_activity(self):
        from pulse_trader import CONN_SHORT, TAG
        from runtime_scope import tracking_scope
        p=Pulse.__new__(Pulse); p.start_eq=100; p.open={}; p.px={'EXT-USDT': 90}
        p.max_book_notional=lambda:1000
        p.cid_ours=lambda cid: str(cid).startswith(TAG)
        p.closed=[
            SimpleNamespace(ours=True,client_id=f'{TAG}own',conn=CONN_SHORT,
                system_id='cts-g',tracking_scope=tracking_scope(CONN_SHORT),
                qty=1,entry=100,pnl=-2,reason='sl',exchange_confirmed=True),
            SimpleNamespace(ours=True,client_id='manual-bot',conn=CONN_SHORT,
                system_id='other-system',tracking_scope='other-system:bingx-x02',
                qty=1,entry=100,pnl=-40,reason='manual',exchange_confirmed=True),
        ]
        p.open['foreign']=SimpleNamespace(ours=True,client_id='manual-bot',connection=CONN_SHORT,
            system_id='other-system',tracking_scope='other-system:bingx-x02',symbol='EXT-USDT',
            side='LONG',qty=1,entry=100)
        activity=p.system_activity()
        self.assertEqual(activity['realized'], -2)
        self.assertEqual(activity['unrealized'], 0)
        self.assertEqual(activity['drawdownPct'], 2)

    def test_unproven_positions_and_conflicting_scope_are_rejected(self):
        from pulse_trader import CONN_SHORT, Position, TAG
        from runtime_scope import row_scope_matches, tracking_scope

        p = Pulse.__new__(Pulse)
        other = "bingx-x01" if CONN_SHORT == "bingx-x02" else "bingx-x02"
        base = dict(symbol="SOL-USDT", side="LONG", qty=1, entry=100,
                    opened_at=1, sl=99, tp=101, peak=100)
        self.assertFalse(p.position_is_ours(Position(**base)))
        self.assertTrue(p.position_is_ours(Position(**base, client_id=f"{TAG}owned")))
        self.assertTrue(p.position_is_ours(Position(**base, tracking_scope=tracking_scope(CONN_SHORT), client_id="manual")))
        self.assertFalse(p.position_is_ours(Position(**base, tracking_scope=tracking_scope(other), client_id=f"{TAG}owned")))
        self.assertFalse(row_scope_matches({
            "trackingScope": tracking_scope(CONN_SHORT),
            "systemId": "other-system",
            "connection": CONN_SHORT,
        }, CONN_SHORT))

    def test_foreign_equity_movement_does_not_latch_system_guard(self):
        from unittest.mock import patch
        from pulse_trader import CONN_SHORT, TAG
        from runtime_scope import tracking_scope

        class FakeApi:
            def __init__(self, equity):
                self.equity = equity

            def get(self, path):
                value = str(self.equity)
                return {"code": 0, "data": {"equity": value, "availableMargin": value,
                    "usedMargin": "0", "unrealizedProfit": "0"}}

        p = Pulse.__new__(Pulse)
        p.api = FakeApi(80)
        p.errors = 0
        p.last_error = ""
        p.equity = p.wallet_equity = p.available = p.used = p.upnl = 0.0
        p.start_eq = p.system_start_eq = 100.0
        p.system_equity = p.system_upnl = 0.0
        p.halted = False
        p.halt_reason = None
        p._pre_pause_halt = None
        p._halt_eq = 0.0
        p.foreign_upnl = -20.0
        p.foreign_realized = 0.0
        p.foreign_position_count = 1
        p.foreign_open_order_count = 1
        p.open = {}
        p.closed = []
        p.px = {}
        p.record_event = lambda *args, **kwargs: None
        p._persist_start_equity = lambda: None
        paths = {name: f"/tmp/cts-g-drawdown-{name}" for name in ("STOP_PATH", "STOP_ALL", "PAUSE_PATH", "RESET_EQ_PATH")}
        with patch.multiple("pulse_trader", **paths):
            p.refresh_balance()
            self.assertFalse(p.halted)
            self.assertAlmostEqual(p.system_equity, 100.0)

            p.api.equity = 60.0
            p.foreign_upnl = -40.0
            p.refresh_balance()
            self.assertFalse(p.halted)
            self.assertAlmostEqual(p.system_equity, 100.0)

            p.api.equity = 120.0
            p.foreign_upnl = 0.0
            p.foreign_realized = 0.0
            p.foreign_position_count = 0
            p.foreign_open_order_count = 0
            p.refresh_balance()
            self.assertFalse(p.halted)
            self.assertAlmostEqual(p.system_equity, 100.0)

            p.api.equity = 140.0
            p.foreign_upnl = 0.0
            p.foreign_realized = 40.0
            p.refresh_balance()
            self.assertFalse(p.halted)
            self.assertAlmostEqual(p.system_equity, 100.0)

            p.api.equity = 120.0
            p.closed = [{"ours": True, "client_id": f"{TAG}loss", "conn": CONN_SHORT,
                "system_id": "cts-g", "tracking_scope": tracking_scope(CONN_SHORT), "pnl": -20.0}]
            p.refresh_balance()
            self.assertTrue(p.halted)
            self.assertEqual(p.halt_reason, "drawdown halt")

    def test_exchange_confirmed_oversized_loss_is_never_hidden(self):

        p=Pulse.__new__(Pulse);p.max_book_notional=lambda:10
        p.cid_ours=lambda cid:cid=='own'
        from pulse_trader import CONN_SHORT
        from runtime_scope import tracking_scope
        p.closed=[SimpleNamespace(ours=True,client_id='own',conn=CONN_SHORT,
            system_id='cts-g',tracking_scope=tracking_scope(CONN_SHORT),
            qty=100,entry=10,pnl=-500,reason='oversized',set_id='s',exchange_confirmed=True)]
        self.assertEqual(p.strategy_closes(),p.closed)
        p.closed[0].ours=False
        self.assertEqual(p.strategy_closes(),[])

if __name__=='__main__':unittest.main()
