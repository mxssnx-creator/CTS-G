"""The crypto universe never silently includes stock/FX aliases or closed contracts."""
import unittest
from select_x02_universe import ordinary_crypto_contract


class Universe(unittest.TestCase):
    def test_ordinary_open_crypto_contract_is_eligible(self):
        self.assertTrue(ordinary_crypto_contract(dict(symbol='BNB-USDT',asset='BNB',status=1,displayName='BNB-USDT',apiStateOpen='true')))

    def test_stock_fx_and_commodity_contracts_are_excluded(self):
        for symbol,asset,display in [('NCSKTSLA2USD-USDT','NCSKTSLA2USD','TSLA-USDT'),
                                     ('NCFXEURUSD2USD-USDT','NCFXEURUSD2USD','EURUSD-USDT'),
                                     ('XAU-USDT','XAU','XAU-USDT')]:
            self.assertFalse(ordinary_crypto_contract(dict(symbol=symbol,asset=asset,status=1,displayName=display)))

    def test_closing_only_and_inactive_contracts_are_excluded(self):
        row=dict(symbol='BNB-USDT',asset='BNB',status=1,displayName='BNB-USDT')
        self.assertFalse(ordinary_crypto_contract({**row,'apiStateOpen':'false'}))
        self.assertFalse(ordinary_crypto_contract({**row,'status':0}))


if __name__=='__main__':unittest.main(verbosity=2)
