"""Independent cash examples for native fills, partials and fee units."""
import unittest
from audit_native_x02 import cash_group,position_cash


def fill(side,qty,price,commission,position='LONG'):
    return dict(side=side,positionSide=position,volume=str(qty),price=str(price),
                commission=str(commission),currency='USDT')


class NativeCash(unittest.TestCase):
    def test_long_partial_exits_pay_each_reported_commission_once(self):
        result=cash_group([fill('BUY',1,100,-.1),fill('SELL',.4,110,-.044),fill('SELL',.6,90,-.054)])
        self.assertEqual(result['gross'],-2)
        self.assertEqual(result['commission'],-.198)
        self.assertEqual(result['net'],-2.198)
        self.assertEqual(result['quantity'],1)

    def test_short_partial_exits_use_the_same_quantity_and_fee_units(self):
        result=cash_group([fill('SELL',1,100,-.1,'SHORT'),fill('BUY',.4,110,-.044,'SHORT'),fill('BUY',.6,90,-.054,'SHORT')])
        self.assertEqual(result['gross'],2)
        self.assertEqual(result['net'],1.802)

    def test_open_inventory_cannot_be_reported_as_settled(self):
        with self.assertRaisesRegex(ValueError,'not flat'):
            cash_group([fill('BUY',1,100,-.1),fill('SELL',.4,110,-.044)])

    def test_nonfinite_values_and_unconverted_commission_currency_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'nonfinite'):cash_group([fill('BUY','NaN',100,-.1)])
        bad=fill('BUY',1,100,-.1);bad['currency']='BTC'
        with self.assertRaisesRegex(ValueError,'currency'):cash_group([bad])

    def test_external_exit_uses_exact_position_cash_and_full_precision_commission(self):
        entry=dict(positionID=202,symbol='CT-USDT',positionSide='SHORT',executedQty='18.90',
                   avgPrice='.10597',time=1000000,clientOrderId='gown')
        row=dict(positionId=202,symbol='CT-USDT',positionSide='SHORT',closeAllPositions=True,
                 positionAmt='18.90',closePositionAmt='18.90',avgPrice='.10597',avgClosePrice='.10662',
                 positionCommission='-.0020089755',realisedProfit='-.0123',netProfit='-.0143',
                 openTime=1000000,updateTime=1100000)
        result=position_cash(entry,[row])
        self.assertEqual(result['net'],-.0142939755)
        self.assertEqual(result['gross'],-.012285)
        self.assertEqual(result['client_id'],'gown')
        for key,value in [('positionAmt','20'),('positionId',999),('avgPrice','.11')]:
            with self.assertRaises(ValueError):position_cash(entry,[dict(row,**{key:value})])


if __name__=='__main__':unittest.main(verbosity=2)
