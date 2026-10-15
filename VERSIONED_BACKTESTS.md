# Versioned technical replay

Research-only; never changes production recommendations or promotes a winner.

## Experiments run together

- Original: frozen current tactical computation plus its extension overlay.
- V2 structure: reserve Avoid for confirmed structural breakdown.
- V2 entry: require a fired trigger and two closes holding its entry level.
- V2 combined: both tests, keeping fixed 14-session holding rules.
- V3 execution: combined V2 with next-session fills, frozen initial stop,
  full-position 2R target, closing-high 2R trailing stop and 14-session maximum.

V3 is an experimental execution model, NOT the full live PM partial-trim policy.
All versions enter the next session, pay 10 bps per side by default and remain
long-or-cash. A weakened Avoid becomes Hold off, not Buy; that relabel alone
cannot create returns in this simulation. Same-bar ambiguity resolves stop-first;
gaps through a stop fill at the open. Trailing updates apply the following bar.

## Run and reproduce

GitHub Actions → **Compare versioned Trading Desk rules** → Run workflow.
Choose a fixed ticker universe, sample dates and validation split before viewing
results. The default universe is a smoke-test selection, not an investment list
or an unbiased historical universe. Delisted securities must be included in a
proper universe study. Changing parameters after viewing validation means that
period is no longer untouched validation.

The workflow archives downloaded adjusted OHLCV, full trade records, input hashes,
runtime version and frozen source files as a GitHub artifact for 90 days. Download
the artifact for permanent retention. It publishes only the summary under the
separate `historical_replay` database key. Live scores under `current` are untouched.
The owner-only `calibration.historical_replay` response feeds Lovable automatically.
Missing or failed runs never create synthetic performance.

For a local replay using already archived adjusted CSVs:

```sh
python backtest_runner.py --data-dir ./data --tickers AAPL MSFT NVDA \
  --start 2024-01-02 --end 2026-10-14 --validation-start 2026-01-02 \
  --output ./new-report.json --confirm-adjusted-ohlcv
```

CSV columns: Date, Open, High, Low, Close, Volume. Require 200 warm-up bars and
complete benchmark-session coverage. No missing ticker is silently omitted.
The output path must be new, preserving older runs.

Development and validation start with fresh unit capital and no carried positions.
Ticker sleeves receive equal starting capital without cross-ticker rebalancing.
Reports include total return, drawdown, exposure, completed trades, win rate,
average trade return, difference from control, cash and SPY buy-and-hold.

This does not reconstruct past AI research, news or fundamental data. Provider
historical adjustments and current-universe selection can create revision and
survivorship biases. Daily bars do not establish intraday order or executable
liquidity. Historical replay and live shadow evidence must remain separate.
