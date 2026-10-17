# Versioned technical replay

Research-only; never changes production recommendations or promotes a winner.

## October 17 audit corrections

- Strategy manifests assign a content-addressed ID to entry/exit/sizing/fill/cost
  and source definitions. "Original" is displayed as Technical control; it is
  not a claim of full historic app parity. Prior artifacts remain immutable.
- Closed-trade counts, win rate and average trade return exclude sample-boundary
  valuations. Those values still contribute to total portfolio return, with a
  disclosed hypothetical terminal liquidation cost.
- Corrected mode emits four independent arms. V3 is an explicit alias of V2
  combined, not a duplicate fifth result. Legacy mode retains five arms.
- App and replay share `position_policy.full_exit_reason`. A stop breach or
  structural exit outranks earnings review; trims/raise-stop remain discretionary.
- The retrospective catch-up path respects the existing ATR hard gate. This is
  a production precedence correction, not a candidate promotion or new threshold.
- Shadow confirmation remains the existing snapshot-based test, not a persisted
  trigger-event state machine. Replay now uses the same prior-date close cutoff
  as live logging. Direct Enter and fresh event-crossing/staged adds must not be
  claimed as implemented by that candidate. Unknown fired flags remain unknown.
- Candidate numeric inputs reject NaN/infinity. Live code fingerprints now cover
  candidate and shared exit-policy source too.
- The replay workflow runs the full test suite, not only replay/storage tests.
- Same-universe equal-capital buy-and-hold return and drawdown accompany cash/SPY.

These changes create new strategy IDs. They must not be presented as an isolated
exit-only test against an older control, nor as untouched validation. No candidate
promotion, numerical starter/add percentages, or automatic trading is introduced.

## Experiments run together

### Corrected default: signal entries / technical invalidation exits

`signal_invalidation` is now the default exit policy. Each version buys at the
next open after its own qualifying signal, provided the entry-time stop is valid.
The Original control does not require the additional two-close confirmation.
V2 entry/combined retain that gate strictly as comparison controls, not promotions.
All versions use the same full-exit conditions: a close at/below the frozen entry
stop, or Avoid with the close below both MA50 and MA200; fill on the next open.
There is no calendar exit, automatic full target sale, or invented trailing level.
V3 therefore duplicates V2 combined under this exit policy, intentionally isolating
the entry comparison. Optional discretionary partial trims, earnings review and
Raise stop instructions cannot be reconstructed here and are not simulated.
This is a technical exit subset, not complete historical live-PM parity.

5/14/30-session checkpoints record gross marked returns only when still held;
they do not sell. End-of-sample liquidation is separately flagged as accounting,
not a sell recommendation. Reports remain research-only; live rules are unchanged.

### Archived legacy experiment (not the actual full strategy)

Run with `--exit-policy legacy_14_session` to reproduce the prior **forced
14-session holding-period experiment**, including its V3 target/trailing rules.
Never compare its results with the corrected policy without showing this label.

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
