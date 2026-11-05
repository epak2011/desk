"""Download and archive a fixed replay dataset, then run the research simulator."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import yfinance as yf
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers")
    parser.add_argument("--universe-file", type=Path)
    parser.add_argument("--cash-proxy", choices=["SGOV"])
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--validation-start", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--series", choices=["current", "prior_years", "broad_universe"], default="current")
    parser.add_argument("--include-event-study", action="store_true")
    parser.add_argument("--study", choices=["standard", "event"], default="standard")
    parser.add_argument("--exit-policy", choices=["signal_invalidation", "legacy_14_session"],
                        default="signal_invalidation")
    args = parser.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end >= datetime.now(timezone.utc).date():
        raise ValueError("Only completed prior-day data may be used.")
    universe = json.loads(args.universe_file.read_text()) if args.universe_file else None
    names = [t for values in universe["sectors"].values() for t in values] if universe else (args.tickers or "").split(",")
    tickers = sorted(set(t.strip().upper() for t in names if t.strip()))
    if not tickers or len(tickers) > 1000 or any(any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-^" for c in t) for t in tickers):
        raise ValueError("Provide 1–1000 valid ticker names.")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    data = args.output_dir / "data"
    data.mkdir()
    coverage = {"requested_count": len(tickers), "downloaded": [], "failed": []}
    for ticker in sorted(set(tickers) | {"SPY", "QQQ"} | ({args.cash_proxy} if args.cash_proxy else set())):
        frame = yf.download(ticker, start=str(start - timedelta(days=450)),
                            end=str(end + timedelta(days=1)), auto_adjust=True,
                            progress=False, threads=False)
        if frame.empty:
            coverage["failed"].append(ticker)
            continue
        if hasattr(frame.columns, "levels"):
            frame.columns = frame.columns.get_level_values(0)
        frame.index.name = "Date"
        frame.to_csv(data / f"{ticker}.csv")
        coverage["downloaded"].append(ticker)
        print(f"Archived {ticker}", flush=True)
    (args.output_dir / "coverage.json").write_text(json.dumps(coverage, indent=2))
    if coverage["failed"]:
        raise ValueError("Incomplete universe; failed inputs are in coverage.json. No headline result published.")
    output = args.output_dir / "report.json"
    subprocess.run([sys.executable, str(ROOT / "backtest_runner.py"), "--data-dir", str(data),
                    "--tickers", *tickers, "--start", args.start, "--end", args.end,
                    "--validation-start", args.validation_start, "--output", str(output),
                    "--exit-policy", args.exit_policy, "--confirm-adjusted-ohlcv",
                    *(["--cash-proxy", args.cash_proxy] if args.cash_proxy else []),
                    *(["--include-event-study"] if args.include_event_study or args.study == "event" else [])], check=True)
    if universe:
        report = json.loads(output.read_text())
        report["universe_selection"] = universe
        output.write_text(json.dumps(report, indent=2, allow_nan=False))
    if args.publish:
        import backend_layer
        import backtest_store
        backtest_store.publish(json.loads(output.read_text()), backend_layer, series=args.series)
        if args.exit_policy == "legacy_14_session":
            print("Archived legacy forced-sale experiment only; active report and live decisions unchanged.")
        else:
            print("Published owner-only replay summary; no calendar exits; live decisions unchanged.")


if __name__ == "__main__":
    main()
