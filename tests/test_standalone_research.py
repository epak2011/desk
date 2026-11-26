import gzip,hashlib,json,tempfile,unittest
from pathlib import Path
from fastapi.testclient import TestClient
import api_service
import standalone_research as research
from strategy_research import ResearchUnavailable

class StandaloneResearchTests(unittest.TestCase):
 def test_public_exact_snapshot_and_existing_route(self):
  c=TestClient(api_service.app);r=c.get('/v1/strategies/standalone');self.assertEqual(r.status_code,200);d=r.json();self.assertFalse(d['eligible_to_implement']);self.assertEqual([r['name'] for r in d['periods'][0]['results']],['Top 1','Top 3','Top 5','SPY','QQQ']);self.assertAlmostEqual(d['periods'][0]['results'][0]['median_annual_return_pct'],14.7375568371244,places=4);self.assertEqual(c.get('/v1/strategies').status_code,200)
 def test_streamlit_uses_each_selected_period_once(self):
  from contextlib import nullcontext
  class View:
   def __init__(self,period):self.period=period;self.calls=0;self.tables=[]
   def selectbox(self,*args,**kwargs):self.calls+=1;return self.period
   def dataframe(self,rows,**kwargs):self.tables.append(rows)
   def expander(self,*args,**kwargs):return nullcontext()
   def __getattr__(self,name):return lambda *args,**kwargs:None
  for period in research.snapshot()['periods']:
   v=View(period['id']);research.render(v);self.assertEqual(v.calls,1);self.assertEqual(v.tables[0][0]['Positive years'],f"{period['results'][0]['positive_years']}/{period['years']}");self.assertEqual(len(v.tables[1]),period['years'])
 def test_wrong_statistics_are_withheld_even_with_updated_digest(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t);d=research.snapshot();d['periods'][0]['results'][0]['average_annual_return_pct']=999;raw=gzip.compress(json.dumps(d).encode());(p/'standalone-january-v1.json.gz').write_bytes(raw);(p/'standalone-january-v1.manifest.json').write_text(json.dumps({'sha256':hashlib.sha256(raw).hexdigest()}))
   with self.assertRaises(ResearchUnavailable):research.load_snapshot(p)
 def test_missing_or_bad_file_is_withheld(self):
  with tempfile.TemporaryDirectory() as t:
   with self.assertRaises(ResearchUnavailable):research.load_snapshot(Path(t))
if __name__=='__main__':unittest.main()
