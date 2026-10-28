# Performance evidence contract

## One engine, distinct evidence

- Production technical control is `original` in the replay schema. The other V2 rows are unpromoted shadow candidates, not automatically the live strategy.
- `GET /v1/operator/performance` requires the existing owner identity. It returns `historical_replay`, `prior_year_replay`, `prospective_trial`, and `forward_portfolio_performance.status=not_established`.
- Both replay objects expose `comparison_summary` and `calendar_comparison`. Existing owner calibration case responses still include the current historical replay. No decision-history bulk read is needed for the new endpoint.
- Streamlit Rules Performance displays the same saved backend reports. Lovable should render this endpoint, never calculate alternative strategy actions or substitute checkpoint averages for portfolio returns.

## Lovable rendering requirements

Show current technical control first, shadow candidates separately, then SPY, QQQ and the same-stock buy-and-hold benchmark. Display actual start/end, return, max drawdown, exposure, and excess vs SPY/QQQ in **percentage points**. Null QQQ means unavailable. Provide separate prior-year and forward-checkpoint sections. Never label a retrospective simulation as verified forward performance. Preserve owner-only authorization. This backend release does not itself publish a Lovable UI change.

## Simulation rules

Adjusted OHLCV; signals computed with only then-available bars; next-open fills; default 10 bps per side; equal initial ticker sleeves; uninvested cash earns zero. No forced 14-session exit. Open positions contribute marked value, with hypothetical terminal liquidation costs, but are not counted as completed trades. Calendar slices carry capital/positions within each development/validation partition. The split explicitly restarts capital and positions; never concatenate it as one unbroken strategy history.

The eight-stock universe is AMD, AVGO, COIN, CRDO, DASH, NVDA, PLTR, TER. It was selected retrospectively. Full-universe prior years before 2023 cannot be represented with the same 200-session warm-up because CRDO first traded in 2022. Missing stocks must not be silently dropped.

## Prospective registration

`experiments/active.json` explicitly selects v4, registered October 28 for a future October 29 start. V3 remains immutable and visibly blocked on its original source mismatch. A source change blocks v4 too; it does not silently rewrite hashes or promote rules. Eligibility does not verify collection or portfolio profitability. This remains the paired entry-event trial, not a completed cash-and-positions live portfolio ledger.

## Repeatability

Use the Compare versioned Trading Desk rules workflow with `series=current` for YTD and `series=prior_years` for a separate historical report. Reports and downloaded CSV inputs are archived with input/source hashes. All results are research-only and no trading rules are automatically selected from returns.
