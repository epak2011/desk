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
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--validation-start", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()
    if end >= datetime.now(timezone.utc).date():
        raise ValueError("Only completed prior-day data may be used.")
    tickers = sorted(set(t.strip().upper() for t in args.tickers.split(",") if t.strip()))
    if not tickers or len(tickers) > 30 or any(any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-^" for c in t) for t in tickers):
        raise ValueError("Provide 1–30 valid ticker names.")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    data = args.output_dir / "data"
    data.mkdir()
    for ticker in sorted(set(tickers) | {"SPY"}):
        frame = yf.download(ticker, start=str(start - timedelta(days=450)),
                            end=str(end + timedelta(days=1)), auto_adjust=True,
                            progress=False, threads=False)
        if frame.empty:
            raise ValueError(f"No data for {ticker}; do not silently omit a security.")
        if hasattr(frame.columns, "levels"):
            frame.columns = frame.columns.get_level_values(0)
        frame.index.name = "Date"
        frame.to_csv(data / f"{ticker}.csv")
    output = args.output_dir / "report.json"
    subprocess.run([sys.executable, str(ROOT / "backtest_runner.py"), "--data-dir", str(data),
                    "--tickers", *tickers, "--start", args.start, "--end", args.end,
                    "--validation-start", args.validation_start, "--output", str(output),
                    "--confirm-adjusted-ohlcv"], check=True)
    if args.publish:
        import backend_layer
        import backtest_store
        backtest_store.publish(json.loads(output.read_text()), backend_layer)
        print("Published owner-only replay summary; live decisions unchanged.")


if __name__ == "__main__":
    main()
