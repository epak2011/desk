import copy
import unittest
import pandas as pd
import validation_backtest as b
import validation_engine as e


def provider(close=(100.,100.),split=(0.,10.),div=(0.,0.)):
    return pd.DataFrame({'Open':close,'High':[p*1.01 for p in close],'Low':[p*.99 for p in close],
        'Close':close,'Adj Close':close,'Volume':[1000.,1000.],'Dividends':div,'Stock Splits':split},
        index=pd.to_datetime(['2024-06-07','2024-06-10']))


class HistoricalAccounting(unittest.TestCase):
    def test_split_normalized_history_is_not_counted_twice(self):
        nominal,_=b.prepare_frame(provider())
        self.assertEqual(list(nominal.Close),[1000.,100.])
        p=e.new_portfolio(1000);p.update(cash=0.,shares={'NVDA':1.})
        r=nominal.iloc[-1]
        out=e.advance_portfolio(p,{'NVDA':{'Open':r.Open,'Close':r.Close,'split':r['Stock Splits'],'dividend':0.}},'2024-06-10',cost_bps=0)
        self.assertEqual(out['shares']['NVDA'],10.)
        self.assertEqual(out['equity'],1000.)

    def test_nominal_volume_and_dollar_volume_are_consistent(self):
        f=provider();nominal,_=b.prepare_frame(f)
        self.assertEqual(list(nominal.Volume),[100.,1000.])
        self.assertEqual(list(nominal.Close*nominal.Volume),list(f.Close*f.Volume))

    def test_dividend_is_cash_not_an_extra_total_return_windfall(self):
        f=provider(close=(100.,99.),split=(0.,0.),div=(0.,1.));f['Adj Close']=[99.,99.]
        nominal,_=b.prepare_frame(f)
        p=e.new_portfolio(100);p.update(cash=0.,shares={'X':1.})
        r=nominal.iloc[-1]
        out=e.advance_portfolio(p,{'X':{'Open':r.Open,'Close':r.Close,'split':1.,'dividend':r.Dividends}},'2024-06-10')
        self.assertEqual(out['equity'],100.)

    def test_prefix_has_no_future_bars_and_uses_historical_share_units(self):
        nominal,adj=b.prepare_frame(provider())
        prefix=b.historical_prefix(nominal,adj,pd.Timestamp('2024-06-07'))
        self.assertEqual(len(prefix),1)
        self.assertEqual(prefix.Close.iloc[-1],1000.)
        self.assertEqual(prefix.Volume.iloc[-1],100.)

    def test_future_split_reconstruction_does_not_mutate_provider(self):
        f=provider();original=f.copy(deep=True);b.prepare_frame(f)
        pd.testing.assert_frame_equal(f,original)

    def test_nonfinite_prices_and_invalid_actions_fail_the_cohort(self):
        for column,value in (('Close',float('nan')),('Stock Splits',-1.),('Dividends',-1.),('Adj Close',0.)):
            f=provider();f.loc[f.index[0],column]=value
            with self.assertRaises(ValueError):b.prepare_frame(f)

    def test_capital_gain_distribution_cannot_be_silently_ignored(self):
        f=provider();f['Capital Gains']=[0.,1.]
        with self.assertRaises(ValueError):b.prepare_frame(f)

    def test_unavailable_full_policy_never_appears_as_zero_return(self):
        spec={'initial_capital':100000.,'cost_scenarios_bps':[10]}
        books={a:{'equity':110000.,'costs':1.,'turnover':100.,'fills_count':1,'open_positions':1,'exposure':.5} for a in e.ARMS}
        result=b.period_summary(spec,[{'session':'2024-01-02','books':{'10':books}}])
        full=next(r for r in result['rows'] if r['arm']=='full_system')
        technical=next(r for r in result['rows'] if r['arm']=='technical_timing')
        self.assertIsNone(full['return_pct']);self.assertAlmostEqual(technical['return_pct'],10.)


if __name__=='__main__':unittest.main()
