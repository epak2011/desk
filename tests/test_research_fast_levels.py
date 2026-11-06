import json
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import tactical
import research_fast_levels as fast

class EquivalentLevelsTests(unittest.TestCase):
    def test_exact_levels_for_ties_gaps_short_series_and_fixed_random_paths(self):
        rng=np.random.default_rng(7182026)
        paths=[pd.Series([100.]*600),pd.Series(np.arange(600,dtype=float)+1),
               pd.Series(np.tile([100.,101.,102.,101.],150)),pd.Series(rng.lognormal(4,.1,600)),
               pd.Series(rng.lognormal(4,.3,1200)),pd.Series([np.nan]*100)]
        paths += [pd.Series(100+np.cumsum(rng.normal(size=n))) for n in (20,59,60,100,300,504,505,900)]
        for values in paths:
            for lookback in (120,504):
                self.assertEqual(tactical.detect_key_levels(values,lookback_days=lookback),fast.detect_key_levels(values,lookback_days=lookback))

    def test_complete_technical_outputs_are_identical_not_just_level_counts(self):
        from test_backtest_runner import bars
        b=bars(260);rng=np.random.default_rng(918);close=100+np.cumsum(rng.normal(size=len(b)))
        b['Close']=close;b['Open']=close+.2;b['High']=close+1;b['Low']=close-1
        original=tactical.apply_extension_execution_overlay(tactical.compute(b,b))
        with patch.object(tactical,'detect_key_levels',fast.detect_key_levels):
            optimized=tactical.apply_extension_execution_overlay(tactical.compute(b,b))
        self.assertEqual(json.dumps(original,sort_keys=True,default=str),json.dumps(optimized,sort_keys=True,default=str))
