import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import parallel_replay
from test_backtest_runner import bars

class CacheTests(unittest.TestCase):
    def test_generated_cache_preserves_dates_states_and_does_not_recompute(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)
            b=bars(240);b.to_csv(path/'X.csv',index_label='Date');b.to_csv(path/'SPY.csv',index_label='Date')
            dates=list(b.index[-3:]);cache=path/'local.pickle';task=('X',directory,dates,str(cache))
            with patch.object(parallel_replay.tactical,'compute',side_effect=lambda a,c:{'action':'hold_off','as_of':str(a.index[-1])}),patch.object(parallel_replay.tactical,'apply_extension_execution_overlay',side_effect=lambda s:s):
                original_levels=parallel_replay.tactical.detect_key_levels
                name,r=parallel_replay.build_one(task)
                self.assertIs(parallel_replay.tactical.detect_key_levels,original_levels)
            self.assertEqual(name,'X');self.assertEqual(list(r),dates)
            with patch.object(parallel_replay.tactical,'compute',side_effect=AssertionError('must reuse exact cache')):
                self.assertEqual(parallel_replay.build_one(task),(name,r))
