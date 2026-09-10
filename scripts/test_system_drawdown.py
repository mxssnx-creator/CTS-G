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
