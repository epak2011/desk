"""Read-only, versioned January basket evidence shared by Streamlit and API."""
from pathlib import Path
from functools import lru_cache
import copy,gzip,hashlib,json,math,statistics
from strategy_research import ResearchUnavailable
ARTIFACTS=Path(__file__).parent/'research_artifacts'

def load_snapshot(directory=ARTIFACTS):
 try:
  raw=(directory/'standalone-january-v1.json.gz').read_bytes();manifest=json.loads((directory/'standalone-january-v1.manifest.json').read_text());digest=hashlib.sha256(raw).hexdigest()
  if digest!=manifest['sha256']:raise ValueError('digest')
  d=json.loads(gzip.decompress(raw))
  if d['schema_version']!=1 or d['study_id']!='january_top_baskets_lt200_v1' or d['research_only'] is not True or d['eligible_to_implement'] is not False:raise ValueError('contract')
  if [p['id'] for p in d['periods']]!=['2000-2025','2010-2025','2020-2025','2000-2009']:raise ValueError('periods')
  for p in d['periods']:
   years=list(range(p['start_year'],p['end_year']+1))
   if [a['year'] for a in p['annual_returns']]!=years or p['years']!=len(years):raise ValueError('calendar')
   if [r['id'] for r in p['results']]!=['top1','top3','top5','spy','qqq']:raise ValueError('strategies')
   for r in p['results']:
    a=[v[r['id']] for v in p['annual_returns']]
    if not all(isinstance(v,(float,int)) and math.isfinite(v) for v in a):raise ValueError('returns')
    expected={'median_annual_return_pct':statistics.median(a),'average_annual_return_pct':statistics.mean(a),'best_year_return_pct':max(a),'worst_year_return_pct':min(a),'positive_years':sum(v>0 for v in a),'years':len(a),'best_year':years[a.index(max(a))],'worst_year':years[a.index(min(a))]}
    if any(not math.isclose(r[k],v,rel_tol=1e-12,abs_tol=1e-10) for k,v in expected.items()):raise ValueError('statistics')
  d['artifact_sha256']=digest;return d
 except (OSError,ValueError,KeyError,TypeError,EOFError) as e:raise ResearchUnavailable('The saved standalone comparison is unavailable. No substitute results are shown.') from e

@lru_cache(maxsize=1)
def _saved():return load_snapshot()
def snapshot():return copy.deepcopy(_saved())

def render(st):
 st.header('January winners versus SPY and QQQ')
 try:d=snapshot()
 except ResearchUnavailable as e:st.error(str(e));return
 st.caption(f"Research saved {d['as_of']} · observations through {d['data_end']} · research only")
 selected=st.selectbox('Comparison period',[p['id'] for p in d['periods']],key='standalone_period')
 p=next(p for p in d['periods'] if p['id']==selected)
 st.write(f"{p['years']} annual observations · dividends reinvested · zero costs/taxes")
 st.dataframe([{'Strategy':r['name'],'Median year (%)':r['median_annual_return_pct'],'Average year (%)':r['average_annual_return_pct'],'Best year (%)':r['best_year_return_pct'],'Worst year (%)':r['worst_year_return_pct'],'Positive years':f"{r['positive_years']}/{r['years']}"} for r in p['results']],hide_index=True,use_container_width=True)
 st.caption(d['metric_note']);st.caption(d['period_note'])
 with st.expander('Exact rule and assumptions'):
  for rule in d['rules']:st.write('• '+rule)
  for limitation in d['limitations']:st.caption(limitation)
 with st.expander('Year-by-year returns'):st.dataframe(p['annual_returns'],hide_index=True,use_container_width=True)
 st.download_button('Download saved comparison',json.dumps(d,indent=2),file_name='january-standalone-comparison.json',mime='application/json')
 st.caption('Evidence version: '+d['artifact_sha256'])
