import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class WorkerCadenceTests(unittest.TestCase):
    def run_cadence(self, timestamp, event='schedule', schedule='5-55/5 13-21 * * 1-5'):
        workflow = Path('.github/workflows/desk-worker.yml').read_text()
        code = workflow.split("python3 - <<'PYTHON'\n", 1)[1].split('\n          PYTHON', 1)[0]
        code = '\n'.join(line[10:] for line in code.splitlines())
        code = code.replace('datetime.now(ZoneInfo("America/New_York"))', f'datetime.fromisoformat("{timestamp}").astimezone(ZoneInfo("America/New_York"))')
        with tempfile.NamedTemporaryFile() as output, patch.dict(os.environ, {'EVENT_NAME':event,'SCHEDULE':schedule,'GITHUB_OUTPUT':output.name}):
            exec(code, {})
            return Path(output.name).read_text().strip() == 'run=true'

    def test_winter_and_summer_market_open(self):
        self.assertTrue(self.run_cadence('2026-11-25T14:30:00+00:00'))
        self.assertTrue(self.run_cadence('2026-07-08T13:30:00+00:00'))

    def test_off_hours_and_weekends_use_hourly_trigger(self):
        self.assertFalse(self.run_cadence('2026-11-25T14:25:00+00:00'))
        self.assertFalse(self.run_cadence('2026-11-25T21:00:00+00:00'))
        self.assertFalse(self.run_cadence('2026-11-28T15:30:00+00:00'))
        self.assertTrue(self.run_cadence('2026-11-28T15:00:00+00:00', schedule='0 * * * *'))

    def test_manual_work_is_not_delayed_by_market_clock(self):
        self.assertTrue(self.run_cadence('2026-11-28T15:30:00+00:00', event='workflow_dispatch'))
        workflow=Path('.github/workflows/desk-worker.yml').read_text()
        self.assertNotIn('/dispatches', workflow)
        self.assertNotIn('if: always()', workflow)
