"""Streamlit presentation of the same public evidence consumed by Lovable."""
from strategy_research import catalog, detail, ResearchUnavailable


def render_strategies(st):
    st.header('Strategies')
    try:
        data = catalog()
        st.caption(f"Research updated {data['as_of']} · observations end {data['data_end']}")
        st.warning(data['decision'])
        names = {s['id']: s['name'] for s in data['strategies']}
        policy = st.selectbox('Strategy', list(names), index=1, format_func=names.get)
        studies = {s['id']: s['name'] for s in data['studies']}
        study = st.selectbox('Universe study', list(studies), format_func=studies.get)
        window = st.selectbox('Test window', data['windows'], index=data['windows'].index('2019-2025'))
        funding_names = {f['id']: f['label'] for f in data['funding_options']}
        funding = st.selectbox('Funding', list(funding_names), format_func=funding_names.get)
        fill = st.selectbox('Fill convention', ['open', 'close'], format_func=lambda s: f'Next session {s}')
        result = detail(policy, study=study, window=window, funding=funding, fill=fill)['result']
        if result.get('funding_note'):
            st.info(result['funding_note'] + ' Compounding is retained; taxes and costs are zero.')
        st.caption(next(s['description'] for s in data['studies'] if s['id'] == study))
        st.subheader('Scope and rules')
        st.write(result['scope'])
        for rule in next(s['rules'] for s in data['strategies'] if s['id'] == policy): st.write('• ' + rule)
        st.subheader('Results · zero costs')
        cols = st.columns(3)
        for col, name, key in zip(cols, ['Strategy ending value', 'Matched SPY', 'Matched annual control'], ['final_value', 'spy_final_value', 'annual_control_value']): col.metric(name, f"${result[key]:,.0f}")
        st.write(f"Deposits: ${result['contributions']:,.0f}. Strategy drawdown: {result['drawdown_pct']:.1f}%; SPY: {result['spy_drawdown_pct']:.1f}%.")
        if 'performance' in result:
            p = result['performance']
            st.caption(f"{p['start_date']} through {p['end_date']}; time-weighted returns exclude additional deposits. Annualization uses elapsed calendar days.")
            st.dataframe([{'Account': name, 'Total return (%)': p[prefix+'total_return_pct'], 'Annualized return (%)': p[prefix+'annualized_return_pct']} for name, prefix in [('Strategy', ''), ('SPY', 'spy_'), ('Annual control', 'annual_control_')]], hide_index=True)
        st.write('Calendar years vs SPY (not percentage of stocks):', result['years_vs_spy'])
        st.dataframe(result['annual_results'], use_container_width=True, hide_index=True)
        st.subheader('Evidence quality')
        st.dataframe(data['quality'], use_container_width=True, hide_index=True)
        for text in data['limitations']: st.write('• ' + text)
        st.subheader('Next steps')
        for text in data['next_steps']: st.write('• ' + text)
        for source in data['sources']: st.markdown(f"[{source['title']}]({source['url']})")
        st.caption('Artifact SHA256: ' + data['artifact_sha256'])
    except ResearchUnavailable as exc:
        st.error(str(exc))
