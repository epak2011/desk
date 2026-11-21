"""Exercise the read-only research API from the actual Docker COPY source set."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class StrategyReleasePackageTests(unittest.TestCase):
    def test_docker_copied_sources_serve_research_without_checkout_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            for line in (ROOT / 'Dockerfile.api').read_text().splitlines():
                if not line.startswith('COPY '):
                    continue
                parts = shlex.split(line)
                *sources, destination = parts[1:]
                dest = target / destination
                dest.mkdir(parents=True, exist_ok=True)
                for source in sources:
                    origin = ROOT / source
                    if origin.is_dir():
                        shutil.copytree(origin, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy2(origin, dest / origin.name)
            code = '''
from pathlib import Path
from fastapi.testclient import TestClient
import api_service, strategy_research
assert Path(api_service.__file__).parent == Path.cwd()
assert Path(strategy_research.__file__).parent == Path.cwd()
client = TestClient(api_service.app)
catalog = client.get('/v1/strategies')
assert catalog.status_code == 200, catalog.text
assert catalog.json()['eligible_to_implement'] is False
result = client.get('/v1/strategies/monthly12_rotate')
assert result.status_code == 200, result.text
assert abs(result.json()['result']['final_value'] - 25671.229301) < 0.00001
assert client.get('/v1/strategies/monthly12_rotate?study=complete_sp500').status_code == 404
'''
            env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'DATABASE_URL', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY')}
            env['PYTHONPATH'] = str(target)
            run = subprocess.run([sys.executable, '-c', code], cwd=target, env=env, text=True, capture_output=True, timeout=45)
            self.assertEqual(run.returncode, 0, run.stderr[-3000:])
