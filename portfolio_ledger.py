"""Research ledger for explicit PM instructions; never infer discretionary fills.

Instructions dated at a completed close execute on the next bar's open. Entry
and add fractions refer to THEN-AVAILABLE CASH; trim fractions refer to shares.
Saved quantities/choices must be supplied by the caller. No calendar exit exists.
"""
import math
import pandas as pd
import position_policy
from backtest_runner import validate_bars


def replay_instructions(bars, instructions, *, states=None, cost_bps=10):
    bars = validate_bars(bars)
    if not math.isfinite(cost_bps) or not 0 <= cost_bps < 1000:
        raise ValueError("Invalid transaction costs.")
    cost, cash, shares, stop = cost_bps / 10000, 1.0, 0.0, None
    pending, fills, curve, unresolved = [], [], [], []
    for day, bar in bars.sort_index().iterrows():
        for row in pending:
            kind = row.get("kind")
            fraction = position_policy.finite_number(row.get("fraction"))
            new_stop = position_policy.finite_number(row.get("stop"))
            if kind in {"entry", "add", "trim"} and (fraction is None or not 0 < fraction <= 1):
                unresolved.append({"signal_date": row["signal_date"], "kind": kind, "reason": "Explicit fraction required; no fill inferred."})
                continue
            if kind in {"entry", "add"}:
                if (kind == "entry" and shares > 0) or (kind == "add" and shares <= 0):
                    raise ValueError("Entry/add does not match position state.")
                if kind == "entry" and (new_stop is None or not 0 < new_stop < float(bar.Open)):
                    unresolved.append({"signal_date": row["signal_date"], "kind": kind, "reason": "Valid explicit entry stop required."})
                    continue
                spend = cash * fraction
                qty = spend / (float(bar.Open) * (1 + cost))
                shares, cash = shares + qty, cash - spend
                if kind == "entry":
                    stop = new_stop
            elif kind in {"trim", "exit"}:
                qty = shares * fraction if kind == "trim" else shares
                cash += qty * float(bar.Open) * (1 - cost)
                shares -= qty
                if shares <= 1e-12:
                    shares, stop = 0.0, None
            elif kind == "raise_stop":
                if shares <= 0 or new_stop is None or (stop is not None and new_stop < stop):
                    unresolved.append({"signal_date": row["signal_date"], "kind": kind, "reason": "Explicit non-decreasing stop required for an open position."})
                    continue
                stop = new_stop
                qty = 0.0
            elif kind == "hold":
                continue
            else:
                raise ValueError("Unknown PM instruction.")
            fills.append({"signal_date": row["signal_date"], "fill_date": str(day.date()),
                          "kind": kind, "shares": qty, "price": float(bar.Open), "saved_stop": stop})
        pending = []
        state = {**((states or {}).get(day) or {}), "price": float(bar.Close)}
        reason = position_policy.full_exit_reason(state, stop) if shares else None
        if reason:
            pending = [{"kind": "exit", "signal_date": str(day.date()), "reason": reason}]
        else:
            pending = [{**row, "signal_date": str(day.date())} for row in instructions.get(day, [])]
        curve.append({"date": day, "equity": cash + shares * float(bar.Close), "cash": cash,
                      "shares": shares, "stop": stop})
    return {"curve": pd.DataFrame(curve).set_index("date"), "fills": fills,
            "unresolved": unresolved, "unfilled_final_close_instructions": pending,
            "open_position_is_valuation_not_sale": shares > 0,
            "scope": "Explicit instruction replay only; missing sizing, partial trim and stop choices are not reconstructed."}
