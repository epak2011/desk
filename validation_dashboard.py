"""Owner dashboard. No synthetic returns or private evidence exported to public pages."""


def render(st, backend):
    import pandas as pd
    st.subheader('Market performance tracker')
    st.caption('Frozen rules, recorded decisions and realistic costs. Paper results are separate from account returns and signal checkpoints.')
    import performance_tracker
    tracker=performance_tracker.read(backend)
    st.caption('Collection health: '+tracker['freshness']['status'].replace('_',' '))
    st.download_button('Download dated results (CSV)',performance_tracker.csv_export(tracker),
        file_name='market-performance.csv',mime='text/csv',key='market-performance-download')
    st.download_button('Download tracker snapshot (JSON)',__import__('json').dumps(tracker,indent=2,allow_nan=False),
        file_name='market-performance.json',mime='application/json',key='market-performance-json')
    render_historical(st,tracker.get('historical'))
    if tracker['forward'].get('monthly_scorecard'):
        with st.expander('Forward monthly scorecard'):
            st.dataframe(pd.DataFrame(tracker['forward']['monthly_scorecard']),hide_index=True,use_container_width=True)
    milestone=tracker['milestones']
    st.caption(f"Forward evidence: {milestone['completed_sessions']} sessions · preliminary review at 252 · confirmatory review at 504")
    st.subheader('Forward paper portfolios')
    report=tracker['forward']
    if report.get('status')=='not_registered':
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


def render_historical(st,historical):
    import pandas as pd
    with st.expander('Historical backtest — current portfolio model',expanded=True):
        if not historical:
            st.info('The matched portfolio-model backtest has not been published yet. Forward observations remain separate.')
            return
        st.caption('Retrospective research on a current-survivor basket. Historical timestamps are simulated; these are not live or actual-account returns.')
        periods=historical['periods']
        name=st.selectbox('Backtest period',list(periods),format_func=lambda x:'2021 onward, capital carried' if x=='full' else '2026, fresh starting capital',key='validation-history-period')
        cost=st.selectbox('Trading cost per side (basis points)',historical['assumptions']['cost_scenarios_bps'],key='validation-history-cost')
        period=periods[name]
        st.caption(f"{period['start']} through {period['end']} · {period['sessions']} sessions · {len(historical['universe'])} stocks")
        fields={'label':'Portfolio','status':'Evidence','return_pct':'Return %','annualized_return_pct':'Annualized return %',
            'excess_vs_spy_pp':'vs SPY (pp)','max_drawdown_pct':'Max drawdown %','annualized_volatility_pct':'Volatility %',
            'average_exposure_pct':'Average exposure %','costs_dollars':'Costs ($)'}
        st.dataframe(pd.DataFrame([{v:r.get(k) for k,v in fields.items()} for r in period['rows'] if r['cost_bps']==cost]),hide_index=True,use_container_width=True)
        curves=pd.DataFrame(period['equity_curves'][str(cost)]).set_index('session')
        st.line_chart(curves)
        st.line_chart((curves/curves.cummax().clip(lower=period['initial_capital'])-1)*100)
        st.write('Calendar-year returns — capital carried within this replay')
        st.dataframe(pd.DataFrame(period['annual_rows'][str(cost)]),hide_index=True,use_container_width=True)
        with st.expander('Monthly returns and assumptions'):
            st.dataframe(pd.DataFrame(period['monthly_rows'][str(cost)]),hide_index=True,use_container_width=True)
            st.write(historical['assumptions'])
            for caveat in historical['limitations']:st.write(caveat)
