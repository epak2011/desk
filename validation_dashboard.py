"""Owner dashboard. No synthetic returns or private evidence exported to public pages."""


def render(st, backend):
    import pandas as pd
    st.subheader('Market validation — prospective paper portfolios')
    st.caption('Frozen rules, recorded decisions and realistic costs. Paper results are separate from account returns and signal checkpoints.')
    with backend.db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT payload FROM engine_review_status WHERE key='validation_performance'")
            row=cur.fetchone()
    report=row[0] if row else {}
    if not report:
        st.info('Validation has not been registered. A future start and frozen policy are required; no returns are available.')
        return
    st.write(report.get('verdict','No demonstrated edge'))
    st.caption(f"{report.get('completed_sessions',0)} completed sessions · {report.get('universe_count','—')} stocks · as of {report.get('as_of','not started')}")
    if not report.get('complete_policy_id') and not report.get('registration',{}).get('complete_policy_id'):
        st.warning('Full-system measurement is unavailable until selection, sizing, risk and execution policy receipts are connected. Technical comparisons alone cannot establish full-system performance.')
    if report.get('status')=='operational_blocked':
        st.warning('Collection failed. Figures remain dated to the last successfully accounted session; inspect the operational incident log.')
    st.caption(report.get('universe_basis',''))
    rows=report.get('rows',[])
    if not rows:return
    base=[r for r in rows if r['cost_bps']==10]
    fields={'label':'Portfolio','status':'Coverage','return_pct':'Return %','excess_vs_spy_pp':'vs SPY (pp)',
        'excess_vs_qqq_pp':'vs QQQ (pp)','max_drawdown_pct':'Max drawdown %',
        'annualized_volatility_pct':'Annualized volatility %','costs_dollars':'Costs ($)',
        'exposure_pct':'Exposure %','open_positions':'Open positions'}
    st.dataframe(pd.DataFrame([{label:r.get(key) for key,label in fields.items()} for r in base]),hide_index=True,use_container_width=True)
    valid={r['arm'] for r in base if r['status']!='incomplete'}
    curves=report.get('equity_curves',[])
    if curves and valid:
        df=pd.DataFrame([{k:v for k,v in d.items() if k=='session' or k in valid} for d in curves]).set_index('session')
        st.line_chart(df)
        # Include the initial cash value in every peak calculation.
        initial=report.get('initial_capital',100000)
        st.line_chart((df/df.cummax().clip(lower=initial)-1)*100)
    with st.expander('Higher-cost sensitivity — 25 basis points per side'):
        st.dataframe(pd.DataFrame([{label:r.get(key) for key,label in fields.items()} for r in rows if r['cost_bps']==25]),hide_index=True,use_container_width=True)
    with st.expander('Accepted versus rejected candidates'):
        comparison=report.get('candidate_comparison',{})
        st.caption(comparison.get('basis','No mature complete-policy candidate cohorts yet.'))
        if comparison.get('rows'):st.dataframe(pd.DataFrame(comparison['rows']),hide_index=True,use_container_width=True)
    with st.expander('Selection, timing, concentration and execution diagnostics'):
        st.dataframe(pd.DataFrame(report.get('comparison_diagnostics',[])),hide_index=True,use_container_width=True)
        st.write(report.get('market_diagnostics',{}))
        st.write(report.get('concentration',{}))
        st.write(report.get('execution_quality',{}))
    with st.expander('Integrity, incidents and formal assessments'):
        st.write(report.get('integrity',{}))
        for incident in report.get('incidents',[])+report.get('operational_incidents',[]):st.write(incident)
        for assessment in report.get('assessments',[]):st.write(assessment)
        st.caption('252-session preliminary review; one 504-session confirmatory assessment. No automatic rule promotion. Variants are exploratory.')
