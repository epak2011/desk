"""Point-in-time technical replay. Research only; never promotes live rules.

Inputs must contain consistently split-adjusted OHLCV for stocks and SPY.
The current frozen Python implementation is the control, not a reconstruction
of an unspecified historical engine or historical AI/fundamental decisions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

import engine_candidates
import tactical


VERSIONS = ("original", "v2_structure", "v2_entry", "v2_combined", "v3_execution")


@dataclass(frozen=True)
class Assumptions:
    cost_bps_per_side: float = 10.0
    holding_sessions: int = 14
    trail_r: float = 2.0
    target_r: float = 2.0

    def __post_init__(self):
        if not math.isfinite(self.cost_bps_per_side) or not 0 <= self.cost_bps_per_side < 1000:
            raise ValueError("Cost must be finite and between 0 and 1000 bps per side.")
        if self.holding_sessions < 1 or self.trail_r <= 0 or self.target_r <= 0:
            raise ValueError("Horizon, trailing risk and target risk must be positive.")


def validate_bars(frame):
    frame = frame.copy().sort_index()
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.has_duplicates:
        raise ValueError("Bars require unique datetime dates.")
    columns = ["Open", "High", "Low", "Close", "Volume"]
    if not set(columns).issubset(frame):
        raise ValueError("Require Date, Open, High, Low, Close, Volume.")
    frame[columns] = frame[columns].apply(pd.to_numeric, errors="raise")
    if not frame[columns].map(math.isfinite).all().all():
        raise ValueError("Non-finite or missing bar values.")
    if (frame[["Open", "High", "Low", "Close"]] <= 0).any().any() or (frame.Volume < 0).any():
        raise ValueError("Invalid prices or volume.")
    if ((frame.High < frame[["Open", "Close", "Low"]].max(axis=1)) |
            (frame.Low > frame[["Open", "Close", "High"]].min(axis=1))).any():
        raise ValueError("Inconsistent OHLC range.")
    return frame


def policy(state, version):
    """Apply each experiment independently; no mutation or current-data lookup."""
    action = str(state.get("action") or "").lower()
    action = {"enter": "enter_now"}.get(action, action)
    candidates = {r["candidate"]: r for r in engine_candidates.shadow_evaluations(state)}
    names = []
    if version in {"v2_structure", "v2_combined", "v3_execution"}:
        names.append("confirmed_structural_avoid")
    if version in {"v2_entry", "v2_combined", "v3_execution"}:
        names.append("confirmed_entry_hold")
    for name in names:
        row = candidates.get(name)
        if row:
            if row.get("eligible") is not True:
                return "unknown"
            action = row["action"]
    return action


def simulate(bars, signals, version, assumptions=Assumptions()):
    """Signals at close can only affect the next session, never that day's OHLC.

    One fully funded long position per ticker; uninvested cash earns zero.
    V3 uses a valid frozen stop, 2R target, closing-high 2R trailing stop,
    structural-exit next open and a maximum holding period. Stop wins when
    both stop and target are touched in the same daily bar.
    """
    bars = validate_bars(bars)
    cost = assumptions.cost_bps_per_side / 10000
    cash, position, pending, equities, trades, decisions = 1.0, None, None, [], [], []

    def close(price, date, reason):
        nonlocal cash, position
        proceeds = position["shares"] * price * (1 - cost)
        trades.append({"entry_date": position["entry_date"], "exit_date": str(date.date()),
                       "signal_date": position["signal_date"], "entry_price": position["entry"],
                       "exit_price": price, "reason": reason,
                       "net_return_pct": (proceeds / position["capital"] - 1) * 100})
        cash, position = proceeds, None

    for date, bar in bars.iterrows():
        if position and pending and pending["kind"] == "exit":
            close(float(bar.Open), date, "structural_exit_next_open")
        elif not position and pending and pending["kind"] == "entry":
            entry = float(bar.Open)
            stop = pending["state"].get("stop")
            valid_stop = isinstance(stop, (int, float)) and math.isfinite(stop) and 0 < stop < entry
            if version != "v3_execution" or valid_stop:
                risk = entry - float(stop) if valid_stop else None
                position = {"entry": entry, "shares": cash / (entry * (1 + cost)),
                            "capital": cash, "entry_date": str(date.date()),
                            "signal_date": pending["date"], "age": 0,
                            "stop": float(stop) if valid_stop else None,
                            "risk": risk, "peak": entry,
                            "target": entry + assumptions.target_r * risk if risk else None}
                cash = 0.0
            else:
                decisions.append({"date": str(date.date()), "action": "skip_invalid_stop"})
        pending = None
        if position:
            position["age"] += 1
            if version == "v3_execution":
                stop, target = position["stop"], position["target"]
                if float(bar.Open) <= stop:
                    close(float(bar.Open), date, "gap_stop")
                elif float(bar.Low) <= stop:
                    close(stop, date, "stop_first_conservative")
                elif float(bar.Open) >= target:
                    close(float(bar.Open), date, "gap_target")
                elif float(bar.High) >= target:
                    close(target, date, "target")
            if position and position["age"] >= assumptions.holding_sessions:
                close(float(bar.Close), date, "max_horizon")
            if position and version == "v3_execution":
                position["peak"] = max(position["peak"], float(bar.Close))
                position["stop"] = max(position["stop"], position["peak"] - assumptions.trail_r * position["risk"])
        state = signals.get(date)
        if state:
            action = policy(state, version)
            decisions.append({"date": str(date.date()), "action": action})
            if not position and action in {"enter_now", "accumulate"}:
                pending = {"kind": "entry", "state": dict(state), "date": str(date.date())}
            elif position and version == "v3_execution" and action == "avoid":
                ma50, ma200 = state.get("ma50"), state.get("ma200")
                if ma50 and ma200 and float(bar.Close) < min(ma50, ma200):
                    pending = {"kind": "exit"}
        equities.append({"date": date, "equity": cash + (position["shares"] * float(bar.Close) if position else 0),
                         "exposed": position is not None})
    if position:
        close(float(bars.Close.iloc[-1]), bars.index[-1], "end_of_sample")
        equities[-1]["equity"] = cash
    return {"curve": pd.DataFrame(equities).set_index("date"), "trades": trades, "decisions": decisions}


def metrics(curve, trades):
    equity = curve.equity
    # Include the initial unit of capital when measuring the first drawdown.
    peaks = equity.cummax().clip(lower=1.0)
    return {"total_return_pct": float((equity.iloc[-1] - 1) * 100),
            "max_drawdown_pct": float(((equity / peaks - 1) * 100).min()),
            "exposure_pct": float(curve.exposed.mean() * 100),
            "round_trips": len(trades),
            "win_rate_pct": 100 * sum(t["net_return_pct"] > 0 for t in trades) / len(trades) if trades else None,
            "avg_trade_return_pct": sum(t["net_return_pct"] for t in trades) / len(trades) if trades else None}


def replay(histories, benchmark, *, start, end, validation_start, assumptions=Assumptions(), compute=None):
    if compute is None:
        def compute(stock, bench):
            state = tactical.compute(stock, bench)
            return tactical.apply_extension_execution_overlay(state) if state else None
    benchmark = validate_bars(benchmark)
    start, end, split = map(pd.Timestamp, (start, end, validation_start))
    if not start < split <= end:
        raise ValueError("Validation start must be later than start and within the sample.")
    dates = benchmark.loc[start:end].index
    if len(dates) < 2 or not len(benchmark.loc[benchmark.index < start]) >= 200:
        raise ValueError("Require sample sessions and at least 200 pre-start benchmark bars.")
    report = {"assumptions": asdict(assumptions), "versions": list(VERSIONS),
              "validation_start": str(split.date()), "results": {},
              "limitations": ["Technical-engine replay only; no reconstructed historical AI, news or fundamental inputs.",
                              "Original means this frozen implementation, not an unspecified historical release.",
                              "Daily-bar ordering is unknown; stop-first is conservative. No liquidity/market-impact model.",
                              "No automatic tuning or promotion. Validation is only untouched if chosen before inspecting it.",
                              "Avoid and Hold off both remain cash in this long-only simulation; no invented short gains.",
                              "Equal-capital ticker sleeves, no cross-ticker rebalancing; universe selection may introduce survivorship bias."]}
    for label, mask in (("development", dates < split), ("validation", dates >= split)):
        report["results"][label] = {}
        period = dates[mask]
        if len(period) < 2:
            raise ValueError("Each period requires at least two sessions.")
        curves = {v: [] for v in VERSIONS}
        trade_sets = {v: [] for v in VERSIONS}
        decision_counts = {v: Counter() for v in VERSIONS}
        for ticker, raw in sorted(histories.items()):
            bars = validate_bars(raw)
            if not period.isin(bars.index).all() or len(bars.loc[bars.index < start]) < 200:
                raise ValueError(f"{ticker}: missing sessions or insufficient warm-up.")
            signals = {}
            for date in period:
                # Each engine call sees only data available by this close.
                state = compute(bars.loc[:date], benchmark.loc[:date])
                if state:
                    state = dict(state)
                    state.update(tactical.shadow_entry_confirmation(bars.loc[:date], state.get("trigger"),
                                 state.get("trigger_fired"), as_of=date + pd.Timedelta(days=1)))
                    signals[date] = state
            for version in VERSIONS:
                result = simulate(bars.loc[period], signals, version, assumptions)
                curves[version].append(result["curve"])
                trade_sets[version].extend({"ticker": ticker, **t} for t in result["trades"])
                decision_counts[version].update(d["action"] for d in result["decisions"])
        if not histories:
            raise ValueError("Provide at least one ticker.")
        for version in VERSIONS:
            curve = pd.concat([c.equity.rename(i) for i, c in enumerate(curves[version])], axis=1).mean(axis=1)
            exposure = pd.concat([c.exposed.rename(i) for i, c in enumerate(curves[version])], axis=1).mean(axis=1)
            report["results"][label][version] = {**metrics(pd.DataFrame({"equity": curve, "exposed": exposure}), trade_sets[version]),
                                                   "trades": trade_sets[version],
                                                   "decision_counts": dict(decision_counts[version])}
        original_return = report["results"][label]["original"]["total_return_pct"]
        for version in VERSIONS:
            report["results"][label][version]["return_lift_vs_original_pct"] = (
                report["results"][label][version]["total_return_pct"] - original_return)
        spy = float(benchmark.loc[period[-1], "Close"] / benchmark.loc[period[0], "Open"] *
                    (1 - assumptions.cost_bps_per_side / 10000) / (1 + assumptions.cost_bps_per_side / 10000) - 1) * 100
        report["results"][label]["benchmarks"] = {"cash_return_pct": 0, "spy_buy_hold_return_pct": spy}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--validation-start", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cost-bps", type=float, default=10)
    parser.add_argument("--confirm-adjusted-ohlcv", action="store_true", required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new artifact to preserve prior runs.")
    def read(ticker):
        if not ticker or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-^" for c in ticker):
            raise ValueError("Invalid ticker filename.")
        return pd.read_csv(args.data_dir / f"{ticker}.csv", parse_dates=["Date"]).set_index("Date")
    histories = {t: read(t) for t in args.tickers}
    report = replay(histories, read("SPY"), start=args.start, end=args.end,
                    validation_start=args.validation_start, assumptions=Assumptions(cost_bps_per_side=args.cost_bps))
    root = Path(__file__).parent
    report["code_sha256"] = {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                             for p in ("tactical.py", "engine_candidates.py", "backtest_runner.py")}
    report["frozen_sources"] = {p: (root / p).read_text() for p in report["code_sha256"]}
    report["runtime"] = {"pandas": pd.__version__}
    report["input_sha256"] = {t: hashlib.sha256((args.data_dir / f"{t}.csv").read_bytes()).hexdigest()
                              for t in sorted(set(args.tickers) | {"SPY"})}
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Saved research-only replay: {args.output}")


if __name__ == "__main__":
    main()
