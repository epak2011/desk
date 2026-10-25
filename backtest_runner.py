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
import position_policy
import research_protocol


VERSIONS = ("original", "v2_structure", "v2_entry", "v2_combined", "v3_execution")
ACTIVE_VERSIONS = VERSIONS[:-1]
EVENT_VERSIONS = ("event_early", "event_confirmed")


@dataclass(frozen=True)
class Assumptions:
    cost_bps_per_side: float = 10.0
    holding_sessions: int = 14
    trail_r: float = 2.0
    target_r: float = 2.0
    exit_policy: str = "signal_invalidation"

    def __post_init__(self):
        if not math.isfinite(self.cost_bps_per_side) or not 0 <= self.cost_bps_per_side < 1000:
            raise ValueError("Cost must be finite and between 0 and 1000 bps per side.")
        if self.holding_sessions < 1 or self.trail_r <= 0 or self.target_r <= 0:
            raise ValueError("Horizon, trailing risk and target risk must be positive.")
        if self.exit_policy not in {"signal_invalidation", "legacy_14_session"}:
            raise ValueError("Unknown exit policy.")


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
    if version in EVENT_VERSIONS:
        return research_protocol.event_entry(state, confirmed=version == "event_confirmed")
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
    The default uses close-confirmed frozen-stop or Avoid/broken-trend exits
    on the following open, without time limits or automatic target sales.
    Only the explicitly selected legacy experiment's V3 uses a 2R target,
    closing-high 2R trailing stop,
    structural-exit next open and a maximum holding period. Stop wins when
    both stop and target are touched in the same daily bar.
    """
    if version not in VERSIONS + EVENT_VERSIONS:
        raise ValueError("Unknown rule version.")
    bars = validate_bars(bars)
    cost = assumptions.cost_bps_per_side / 10000
    legacy = assumptions.exit_policy == "legacy_14_session"
    cash, position, pending, equities, trades, decisions = 1.0, None, None, [], [], []
    used_events = set()

    def close(price, date, reason):
        nonlocal cash, position
        proceeds = position["shares"] * price * (1 - cost)
        trades.append({"entry_date": position["entry_date"], "exit_date": str(date.date()),
                       "signal_date": position["signal_date"], "entry_price": position["entry"],
                       "exit_price": price, "reason": reason,
                       "exit_is_sample_boundary": reason == "end_of_sample",
                       "checkpoint_gross_returns_pct": dict(position["checkpoints"]),
                       "gross_traded_notional": position["shares"] * (position["entry"] + price),
                       "net_return_pct": (proceeds / position["capital"] - 1) * 100})
        cash, position = proceeds, None

    for date, bar in bars.iterrows():
        if position and pending and pending["kind"] == "exit":
            close(float(bar.Open), date, pending.get("reason", "structural_exit_next_open"))
        elif not position and pending and pending["kind"] == "entry":
            entry = float(bar.Open)
            stop = pending["state"].get("stop")
            valid_stop = isinstance(stop, (int, float)) and math.isfinite(stop) and 0 < stop < entry
            if (legacy and version != "v3_execution") or valid_stop:
                risk = entry - float(stop) if valid_stop else None
                position = {"entry": entry, "shares": cash / (entry * (1 + cost)),
                            "capital": cash, "entry_date": str(date.date()),
                            "signal_date": pending["date"], "age": 0,
                            "stop": float(stop) if valid_stop else None,
                            "risk": risk, "peak": entry,
                            "checkpoints": {},
                            "target": entry + assumptions.target_r * risk if risk else None}
                cash = 0.0
                if pending.get("event_id"):
                    used_events.add(pending["event_id"])
            else:
                decisions.append({"date": str(date.date()), "action": "skip_invalid_stop"})
        pending = None
        if position:
            position["age"] += 1
            if legacy and version == "v3_execution":
                stop, target = position["stop"], position["target"]
                if float(bar.Open) <= stop:
                    close(float(bar.Open), date, "gap_stop")
                elif float(bar.Open) >= target:
                    close(float(bar.Open), date, "gap_target")
                elif float(bar.Low) <= stop:
                    close(stop, date, "stop_first_conservative")
                elif float(bar.High) >= target:
                    close(target, date, "target")
            if position and legacy and position["age"] >= assumptions.holding_sessions:
                close(float(bar.Close), date, "max_horizon")
            if position and legacy and version == "v3_execution":
                position["peak"] = max(position["peak"], float(bar.Close))
                position["stop"] = max(position["stop"], position["peak"] - assumptions.trail_r * position["risk"])
            if position and position["age"] in {5, 14, 30}:
                position["checkpoints"][str(position["age"])] = (
                    float(bar.Close) / position["entry"] - 1) * 100
        state = signals.get(date)
        if state:
            state = dict(state)
            if version in EVENT_VERSIONS and state.get("entry_event"):
                state["stop"] = state["entry_event"].get("stop")
            action = policy(state, version)
            decisions.append({"date": str(date.date()), "action": action})
            if not position and action in {"enter_now", "accumulate"}:
                event_id = (state.get("entry_event") or {}).get("event_id") if version in EVENT_VERSIONS else None
                if event_id not in used_events:
                    pending = {"kind": "entry", "state": dict(state), "date": str(date.date()), "event_id": event_id}
            elif position and legacy and version == "v3_execution" and action == "avoid":
                ma50, ma200 = state.get("ma50"), state.get("ma200")
                if ma50 and ma200 and float(bar.Close) < min(ma50, ma200):
                    pending = {"kind": "exit"}
        # A close-confirmed invalidation is executable only on the next open.
        # Never move the saved stop down using a freshly recomputed setup stop.
        if position and not legacy:
            exit_state = {**(state or {}), "price": float(bar.Close)}
            if state:
                exit_state["action"] = action
            reason = position_policy.full_exit_reason(exit_state, position["stop"])
            if reason:
                pending = {"kind": "exit", "reason": reason + "_exit_next_open"}
        equities.append({"date": date, "equity": cash + (position["shares"] * float(bar.Close) if position else 0),
                         "exposed": position is not None})
    if position:
        close(float(bars.Close.iloc[-1]), bars.index[-1], "end_of_sample")
        equities[-1]["equity"] = cash
    return {"curve": pd.DataFrame(equities).set_index("date"), "trades": trades, "decisions": decisions}


def metrics(curve, trades, sleeves=1):
    equity = curve.equity
    # Include the initial unit of capital when measuring the first drawdown.
    peaks = equity.cummax().clip(lower=1.0)
    closed = [t for t in trades if not t.get("exit_is_sample_boundary")]
    return {"total_return_pct": float((equity.iloc[-1] - 1) * 100),
            "max_drawdown_pct": float(((equity / peaks - 1) * 100).min()),
            "exposure_pct": float(curve.exposed.mean() * 100),
            "round_trips": len(closed),
            "closed_trade_count": len(closed),
            "sample_boundary_valuation_count": len(trades) - len(closed),
            "gross_turnover_pct": sum(t["gross_traded_notional"] for t in trades) / sleeves * 100,
            "win_rate_pct": 100 * sum(t["net_return_pct"] > 0 for t in closed) / len(closed) if closed else None,
            "avg_trade_return_pct": sum(t["net_return_pct"] for t in closed) / len(closed) if closed else None,
            "valuation_basis": "Open positions included in total return at sample-end value less hypothetical liquidation costs; excluded from closed-trade statistics."}


def matched_exposure_benchmark(benchmark, weights, cost_bps):
    """SPY/cash with the strategy's already-decided open exposure schedule.

    Previous weight owns the overnight gap; new weight owns open-to-close.
    Charge turnover at each open and terminal valuation cost. This is an ex-post
    exposure-matched diagnostic, NOT an independently investable timing signal.
    """
    cost, equity, prior_weight = cost_bps / 10000, 1.0, 0.0
    prior_close = float(benchmark.Open.iloc[0])
    values = []
    for dt, bar in benchmark.iterrows():
        weight = float(weights.loc[dt])
        if not math.isfinite(weight) or not 0 <= weight <= 1:
            raise ValueError("Exposure weight must be in [0, 1].")
        gap_ratio = float(bar.Open) / prior_close
        gap_factor = 1 + prior_weight * (gap_ratio - 1)
        equity *= gap_factor
        pretrade_weight = prior_weight * gap_ratio / gap_factor
        equity *= 1 - abs(weight - pretrade_weight) * cost
        intraday_ratio = float(bar.Close) / float(bar.Open)
        intraday_factor = 1 + weight * (intraday_ratio - 1)
        equity *= intraday_factor
        values.append(equity)
        prior_weight, prior_close = weight * intraday_ratio / intraday_factor, float(bar.Close)
    values[-1] *= 1 - prior_weight * cost
    curve = pd.Series(values, index=benchmark.index)
    return {"total_return_pct": float((curve.iloc[-1] - 1) * 100),
            "max_drawdown_pct": float(((curve / curve.cummax().clip(lower=1) - 1) * 100).min()),
            "basis": "Ex-post SPY/cash matched to mean equal-sleeve exposure at open; gap uses prior weight; turnover costs included."}


def replay(histories, benchmark, *, start, end, validation_start, assumptions=Assumptions(), compute=None,
           include_event_study=False):
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
    versions = VERSIONS if assumptions.exit_policy == "legacy_14_session" else ACTIVE_VERSIONS
    if include_event_study:
        if assumptions.exit_policy != "signal_invalidation":
            raise ValueError("Event study requires the corrected shared exit policy.")
        versions += EVENT_VERSIONS
    source_files = ("tactical.py", "engine_candidates.py", "backtest_runner.py", "position_policy.py", "research_protocol.py")
    frozen_sources = {p: (Path(__file__).parent / p).read_text() for p in source_files}
    source_hashes = {p: hashlib.sha256(text.encode()).hexdigest() for p, text in frozen_sources.items()}
    manifests = {}
    for version in versions:
        manifest = {
            "label": "Technical control" if version == "original" else version,
            "entry_policy": version, "exit_policy": assumptions.exit_policy,
            "shared_exit_version": position_policy.POLICY_VERSION,
            "sizing_policy": "full equal-capital ticker sleeve; NOT live starter/portfolio sizing",
            "fill_policy": "signal close then next open; no same-bar decision fills",
            "cost_bps_per_side": assumptions.cost_bps_per_side,
            "confirmation_basis": ("Observed close crossing of a previously armed frozen level; confirmed arm waits for two subsequent held closes."
                                   if version in EVENT_VERSIONS else
                                   "snapshot fired flag plus two prior-date closes versus snapshot level; not two closes after a persisted trigger event"),
            "source_sha256": source_hashes,
            "legacy_parameters": asdict(assumptions) if assumptions.exit_policy == "legacy_14_session" else None,
            "scope": "technical-only; no historical AI/fundamentals/discretionary PM"}
        if version in EVENT_VERSIONS:
            manifest["control_comparison"] = "Same canonical qualifying action, frozen event stop, next-open fill and costs; only post-event confirmation differs."
        manifest["strategy_id"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        manifests[version] = manifest
    published_assumptions = (asdict(assumptions) if assumptions.exit_policy == "legacy_14_session" else {
        "cost_bps_per_side": assumptions.cost_bps_per_side,
        "exit_policy": assumptions.exit_policy,
        "calendar_exit_enabled": False,
        "performance_checkpoints_sessions": [5, 14, 30],
    })
    report = {"assumptions": published_assumptions, "versions": list(versions),
              "code_sha256": source_hashes, "frozen_sources": frozen_sources,
              "strategy_manifests": manifests,
              "version_aliases": {"v3_execution": "v2_combined"} if "v3_execution" not in versions else {},
              "experiment": ("signal entries / technical invalidation exits"
                             if assumptions.exit_policy == "signal_invalidation"
                             else "forced 14-session holding-period experiment"),
              "sample_start": str(start.date()), "sample_end": str(end.date()),
              "universe": sorted(histories),
              "validation_start": str(split.date()), "results": {},
              "limitations": ["Technical-engine replay only; no reconstructed historical AI, news or fundamental inputs.",
                              "Original means this frozen implementation, not an unspecified historical release.",
                              "Daily-bar ordering is unknown; stop-first is conservative. No liquidity/market-impact model.",
                              "No automatic tuning or promotion. Validation is only untouched if chosen before inspecting it.",
                              "Avoid and Hold off both remain cash in this long-only simulation; no invented short gains.",
                              "Equal-capital ticker sleeves, no cross-ticker rebalancing; universe selection may introduce survivorship bias."]}
    if assumptions.exit_policy == "signal_invalidation":
        report["limitations"].extend([
            "Full exits use a frozen entry stop breached at close or Avoid below both MA50/MA200, filled next open. No historical earnings gate is reconstructed.",
            "Discretionary partial trims and Raise stop instructions are not simulated; this is not the complete live PM process.",
            "V3 has the same exit policy as V2 combined in this controlled comparison; its legacy targets/trailing/time limit are disabled.",
            "Sample-boundary liquidation is an accounting convention, not a sell signal. Checkpoints are gross marked returns for positions still held at 5/14/30 sessions."])
    for label, mask in (("development", dates < split), ("validation", dates >= split)):
        report["results"][label] = {}
        period = dates[mask]
        if len(period) < 2:
            raise ValueError("Each period requires at least two sessions.")
        curves = {v: [] for v in versions}
        trade_sets = {v: [] for v in versions}
        decision_counts = {v: Counter() for v in versions}
        passive_curves = []
        for ticker, raw in sorted(histories.items()):
            bars = validate_bars(raw)
            if not period.isin(bars.index).all() or len(bars.loc[bars.index < start]) < 200:
                raise ValueError(f"{ticker}: missing sessions or insufficient warm-up.")
            signals = {}
            journal = {}
            sample_bars = bars.loc[period]
            cost = assumptions.cost_bps_per_side / 10000
            passive = sample_bars.Close / (sample_bars.Open.iloc[0] * (1 + cost))
            passive.iloc[-1] *= 1 - cost
            passive_curves.append(passive)
            for date in period:
                # Each engine call sees only data available by this close.
                state = compute(bars.loc[:date], benchmark.loc[:date])
                if state:
                    state = dict(state)
                    if include_event_study:
                        journal = research_protocol.advance_event(journal, ticker=ticker,
                            session=str(date.date()), close=float(bars.loc[date, "Close"]),
                            state=state, fingerprint=research_protocol.identity(source_hashes))
                        state["entry_event"] = dict(journal.get("event") or {})
                    state.update(tactical.shadow_entry_confirmation(bars.loc[:date], state.get("trigger"),
                                 state.get("trigger_fired"), as_of=date))
                    signals[date] = state
            for version in versions:
                result = simulate(bars.loc[period], signals, version, assumptions)
                curves[version].append(result["curve"])
                trade_sets[version].extend({"ticker": ticker, **t} for t in result["trades"])
                decision_counts[version].update(d["action"] for d in result["decisions"])
        if not histories:
            raise ValueError("Provide at least one ticker.")
        for version in versions:
            curve = pd.concat([c.equity.rename(i) for i, c in enumerate(curves[version])], axis=1).mean(axis=1)
            exposure = pd.concat([c.exposed.rename(i) for i, c in enumerate(curves[version])], axis=1).mean(axis=1)
            report["results"][label][version] = {**metrics(pd.DataFrame({"equity": curve, "exposed": exposure}), trade_sets[version], len(histories)),
                                                   "matched_exposure_spy": (matched_exposure_benchmark(benchmark.loc[period], exposure, assumptions.cost_bps_per_side)
                                                                            if assumptions.exit_policy == "signal_invalidation" else None),
                                                   "trades": trade_sets[version],
                                                   "decision_counts": dict(decision_counts[version])}
        original_return = report["results"][label]["original"]["total_return_pct"]
        for version in versions:
            report["results"][label][version]["return_lift_vs_original_pct"] = (
                report["results"][label][version]["total_return_pct"] - original_return)
        spy = float(benchmark.loc[period[-1], "Close"] / benchmark.loc[period[0], "Open"] *
                    (1 - assumptions.cost_bps_per_side / 10000) / (1 + assumptions.cost_bps_per_side / 10000) - 1) * 100
        basket = pd.concat(passive_curves, axis=1).mean(axis=1)
        report["results"][label]["benchmarks"] = {
            "cash_return_pct": 0, "spy_buy_hold_return_pct": spy,
            "same_universe_buy_hold_return_pct": float((basket.iloc[-1] - 1) * 100),
            "same_universe_buy_hold_max_drawdown_pct": float(((basket / basket.cummax().clip(lower=1) - 1) * 100).min())}
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
    parser.add_argument("--include-event-study", action="store_true")
    parser.add_argument("--exit-policy", choices=["signal_invalidation", "legacy_14_session"],
                        default="signal_invalidation")
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
                    validation_start=args.validation_start, assumptions=Assumptions(
                        cost_bps_per_side=args.cost_bps, exit_policy=args.exit_policy),
                    include_event_study=args.include_event_study)
    # Sources were frozen before computation, not re-read after a long run.
    report["runtime"] = {"pandas": pd.__version__}
    report["input_sha256"] = {t: hashlib.sha256((args.data_dir / f"{t}.csv").read_bytes()).hexdigest()
                              for t in sorted(set(args.tickers) | {"SPY"})}
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Saved research-only replay: {args.output}")


if __name__ == "__main__":
    main()
