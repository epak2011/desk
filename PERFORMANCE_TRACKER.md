# Track and backtest market performance

The owner-only internal Rules Performance page now contains a Market performance tracker. Historical backtests, forward paper portfolios and signal checkpoints have separate labels. Neither a backtest nor a checkpoint average can become actual-account or prospective portfolio performance by renaming it.

## Tracking

- Live equity and drawdown curves, same-period SPY/QQQ excess, costs, turnover, exposure and holdings counts come from the unchanged registered paper ledger.
- Monthly forward scorecards use the previous month's ending equity as their opening capital. Incomplete arms are excluded, not assigned zero return.
- Collection health uses the exchange calendar and a two-hour settled-close allowance. Holidays and a registered future start do not create false stale-data warnings.
- CSV and JSON downloads keep study/protocol IDs, date ranges, cost settings and evidence types. Formal review progress remains at 252/504 sessions; no daily winner promotion is introduced.
- `GET /v1/operator/performance/tracker` returns the combined owner view. `GET /v1/operator/performance/tracker.csv` downloads dated rows. Both require the verified owner session. The separate Lovable client can render these endpoints; its source is not present in this repository and is not changed here.
- A lightweight tracker refresh runs after each production collection workflow, including failures. The API computes freshness at read time, without altering the underlying ledger or its frozen sources.

## Historical replay

`scripts/download_validation_inputs.py` archives all 110 stock histories plus SPY/QQQ/SGOV. `scripts/run_validation_backtest.py` uses the existing shared-cash `validation_engine` for the seven reconstructable portfolios at 10 and 25 bps per side. Full-system and system-selected passive allocations remain unavailable because historical complete-policy receipts do not exist. No historic AI/fundamental/macro allocation is invented.

The initial study covers January 4, 2021 through November 5, 2026, with at least 200 warm-up observations and a preceding-close seed. A separately labeled January 2, 2026 reset starts with fresh $100,000 capital; annual returns within the full study carry capital. This extends the exploratory evidence through the 2022 decline. It does not remove the current-survivor selection bias or provide an untouched holdout.

Yahoo `auto_adjust=False` OHLCV is still split-normalized. The replay reconstructs nominal share units from returned split events before explicitly changing share quantities and crediting dividends. It also restores corresponding volume units. Indicators use only the prefix through each signal date, normalized to that date's price/share units. This avoids a ten-for-one split creating a fictitious tenfold gain. Adjusted indicator data and nominal portfolio accounting remain separate. Dividends are cash at the ex date, cash earns the same disclosed SGOV proxy, and no dividend is also credited as adjusted-price appreciation in the wallet.

These are reconstructed vendor histories, not an original exchange tape. Provider revisions, complex spin-offs, bankruptcy settlement, taxes and actual corporate-action pay dates are not fully reconstructed. No constituent is silently dropped for missing data. The full input manifest, source hashes, exact provider files and equivalence checks accompany the local reproduction archive.

Compared with the earlier isolated-sleeve replay, this study uses shared cash, explicit portfolio rebalances, raw action accounting and historical-unit indicators. It is a distinct experiment. Different results must not be presented as a correction of an identical old experiment or as prospective performance.

### Reproduce

```bash
python scripts/download_validation_inputs.py --start 2021-01-04 --end 2026-11-05 --output-dir /new/archive/path
python scripts/run_validation_backtest.py --dataset /new/archive/path --start 2021-01-04 --end 2026-11-05 --reset-start 2026-01-02 --workers 4 --output /new/archive/path/report.json
```

Choose a new output path for every report. Locally generated signal caches are a calculation aid, not inputs to accept from strangers. `--publish` writes only `validation_suite_replay` and an immutable study archive, never the prospective ledger or old historical report pointers. The manual **Backtest current validation portfolios** GitHub workflow reproduces and publishes a requested range, preserving the 113-file cohort requirement and a 90-day workflow artifact. Long-term copies of the public input/source archive are also supplied locally.

Historical charts and CSV exports are downloadable snapshots. Forward tracking begins with the registered November 9 US session; at implementation time no mature forward outcome exists. A historical winner can guide a new preregistered test but cannot establish that this system beats the market.

Data-convention references: [yfinance price repair documentation](https://ranaroussi.github.io/yfinance/advanced/price_repair.html), [upstream adjustment discussion](https://github.com/ranaroussi/yfinance/issues/687). The actual provider frames and split-continuity fixtures, rather than documentation alone, are used to validate this adapter.

## Publish a completed study without recalculating

The completed study's compressed summary is checked into `research/results/` with SHA256 hashes. `python scripts/publish_validation_study.py --verify-only` checks the archive, uncompressed content, declared evidence type, all calculation-source hashes, 113 input identifiers and every reviewed result row. The **Publish verified validation study** workflow runs those checks and publishes only the historical pointer. It never writes the forward ledger. This is publication of completed local research, not an independently recomputed hosted backtest.

Full recalculation remains available through **Backtest current validation portfolios**. The 2021–2026 replay takes roughly two hours on the local four-worker setup; the hosted workflow allows up to six hours. The publication workflow avoids that duplicate calculation when the reviewed study is already complete.
