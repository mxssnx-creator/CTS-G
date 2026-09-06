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
    def test_exchange_confirmed_oversized_loss_is_never_hidden(self):
        p=Pulse.__new__(Pulse);p.max_book_notional=lambda:10
        p.cid_ours=lambda cid:cid=='own'
        from pulse_trader import CONN_SHORT
        p.closed=[SimpleNamespace(ours=True,client_id='own',conn=CONN_SHORT,
            qty=100,entry=10,pnl=-500,reason='oversized',set_id='s',exchange_confirmed=True)]
        self.assertEqual(p.strategy_closes(),p.closed)
        p.closed[0].ours=False
        self.assertEqual(p.strategy_closes(),[])

if __name__=='__main__':unittest.main()
