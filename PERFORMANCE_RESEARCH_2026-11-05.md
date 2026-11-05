# Expanded performance research

The earlier replay and independent entry-event registration used a manually selected eight-name cohort (AMD, AVGO, COIN, CRDO, DASH, NVDA, PLTR, TER). They were not the app's entire ticker population. The historical replay downloader also imposed a 30-name request cap. New research requests accept up to 1,000 names. The existing eight-name trials remain frozen; no original observations, registration IDs, worker, rules, sizing or live trading decisions are rewritten.

## Coverage and universe

`audit_performance_coverage.py` uses aggregate SQL over **all** durable decision rows to count actual app tickers, records, date coverage and recorded engine versions, without downloading full decisions or exposing user ticker lists. Coverage counts are not independent performance samples.

`research/broad-universe-2026-11-05.json` predefines 110 names: ten per sector across eleven sectors. This is a larger, mixed-sector research universe, not a reconstructed historical index. Choosing surviving companies today retains survivorship/selection bias. The historical test is explicitly exploratory, including its 2026 comparison slice; it is not untouched validation. Missing prices or insufficient warm-up fail the complete-universe headline rather than silently dropping names. Archives include exact OHLCV, coverage and source hashes. These are not 110 investment recommendations.

## Matched historical benchmarks and attribution

All strategy arms and SPY, QQQ and same-stock buy-and-hold comparisons use identical sessions, starting capital and per-side trading-cost settings. Idle cash uses SGOV adjusted total-return accrual rather than an undisclosed zero-yield assumption. SGOV is an expense-bearing ETF proxy, not a guaranteed or executable bank yield; no tax/market-impact model is claimed. The old zero-cash runs remain distinct archived reports. Broad results have a separate owner-only pointer and never replace the old eight-stock report.

Entry/exit attribution has two views:

- Exact per-stock daily log-return differences versus holding the same name: initial waiting, entry days, invested days, exit days and cash intervals between trades. Those buckets sum in log points. Their cross-name mean is descriptive and must not be called arithmetic portfolio alpha or a causal estimate of a proposed rule change.
- 5/14/30-session stock returns after actual entry and full-exit fills, including positions that were sold before a checkpoint. Positive post-exit returns describe foregone upside; negative returns describe avoided losses. Immature event denominators and overlapping-window limitations are explicit. They do not mandate a calendar sale or justify automatic tuning.

## Forward paper ledger

A separate 110-name paper protocol is registered with a future start, November 6, 2026. Its ID covers fixed universe, costs, source hashes and registration timestamp. No historic signal is recomputed to fill a missing observation; late or missing observations are reported, and incomplete coverage suppresses the strategy-return headline. A changed source or edited registration blocks collection instead of silently changing the experiment.

The collector is independent of Claude/research generation and the frozen worker. It records actual completed-close rule outputs. Only orders recorded before an actual next-session 9:30 ET open can fill that open. Ledger sessions and observations are append-only and serialized under a transaction lock; reruns cannot overwrite them. Each session saves raw bars, cash factors, cash balances, shares, frozen stops, pending orders and accounting values. Raw split and cash-dividend accounting are explicit. Benchmark portfolios start on the same predetermined open, pay the same initial trading-cost rule, retain cash dividends and earn the same SGOV proxy on cash. Open positions remain marked; there is no 14-session forced sale or invented discretionary trim.

This is hypothetical technical-sleeve performance, **not actual account returns or the complete discretionary PM strategy**. Newly collected sessions do not establish a mature edge. Old checkpoint means remain separate. Owner-only `GET /v1/operator/performance` adds `decision_history_coverage`, `broad_universe_replay` and `forward_paper_performance`; the existing `forward_portfolio_performance` continues to say actual forward portfolio performance is not established.

The daily collector and publishing changes require review/merge before activation. No historical result is labeled prospective, and no candidate is promoted automatically.

## Coverage findings and revised cohort

Hosted aggregate audit on November 5: 16,393 decision records, 46 distinct tickers, 1,751 rows with stored outcomes; records span May 2–November 5. These counts include repeat calls, differing engine versions and immature observations, not 16,393 independent trades. Hosted tests passed.

The first 110-name preset failed complete-universe coverage: AVB and WBD had been acquired/delisted, and the public provider returned unusable one-row histories. That failed dataset and original preset are retained. Before viewing any complete cohort performance, the v2 preset explicitly substitutes ESS for AVB and TTD for WBD. All 110 v2 stock inputs plus SPY/QQQ/SGOV passed full sample-session and warm-up coverage. Selection remains survivor-biased and exploratory; replacement is disclosed, not silently omitted. The prospective v2 registration is separate, retains the original protocol ID as provenance and begins November 6. The original unstarted registration is not rewritten.

The existing eight-name event-study health check currently reports a frozen worker-source mismatch on the base release; this work does not claim to repair or backfill that cohort. The new paper collector is separately frozen and independent of that worker.
