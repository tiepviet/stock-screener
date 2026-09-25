"""Safe conversion helpers for domain objects and pandas/numpy values.

Domain modules return convenient dataclasses, enums, pandas objects and numpy
scalars.  Passing those values directly to Starlette's JSON encoder can either
fail or emit non-standard ``NaN``/``Infinity`` values.  Every API response
passes through these helpers so responses remain valid JSON and never contain
provider-specific objects.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime
from enum import Enum
from os import PathLike
from typing import Any

import numpy as np
import pandas as pd

from src.stock_screener.multi_timeframe import ConfirmedSignal
from src.stock_screener.portfolio import PortfolioTracker
from src.stock_screener.price_target import PriceTargets
from src.stock_screener.technical_engine import Signal


def _is_missing(value: Any) -> bool:
    """Return true for scalar missing values without ambiguous array truth."""

    if value is None:
        return True
    if isinstance(value, float):
        return not math.isfinite(value)
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if isinstance(missing, (bool, np.bool_)):
        return bool(missing)
    return False


def safe_value(value: Any) -> Any:
    """Recursively convert a value to a strict JSON-compatible structure."""

    if _is_missing(value):
        return None
    if isinstance(value, Enum):
        return safe_value(value.value)
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, np.generic):
        return safe_value(value.item())
    if dataclasses.is_dataclass(value):
        return {
            str(key): safe_value(item)
            for key, item in dataclasses.asdict(value).items()
        }
    if isinstance(value, dict):
        return {str(key): safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_value(item) for item in value]
    if isinstance(value, pd.DataFrame):
        return safe_dataframe(value)
    if isinstance(value, pd.Series):
        return [safe_value(item) for item in value.tolist()]
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, PathLike):
        return str(value)
    return str(value)


def safe_dataframe(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    """Serialize a DataFrame with its index represented as a string field."""

    if df is None or df.empty:
        return []
    frame = df.tail(limit) if limit is not None else df
    result: list[dict[str, Any]] = []
    for index, row in frame.iterrows():
        item: dict[str, Any] = {}
        # A chart response needs a stable date field even when the source
        # index has no name.  Do not mutate the caller's DataFrame.
        if isinstance(index, (pd.Timestamp, datetime, date)):
            item["date"] = safe_value(index)
        else:
            item["index"] = safe_value(index)
        for column, value in row.items():
            item[str(column)] = safe_value(value)
        result.append(item)
    return result


def signal_dict(signal: Signal) -> dict[str, Any]:
    """Serialize a technical signal with a few stable aliases."""

    return safe_value(
        {
            "ticker": signal.ticker,
            "signal_type": signal.signal_type.value,
            "type": signal.signal_type.value,
            "strategy": signal.strategy,
            "date": signal.date,
            "price": signal.price,
            "stop_loss": signal.stop_loss,
            "metadata": signal.metadata,
        }
    )


def confirmed_signal_dict(signal: ConfirmedSignal) -> dict[str, Any]:
    return safe_value(
        {
            "ticker": signal.ticker,
            "signal_type": signal.signal_type.value,
            "type": signal.signal_type.value,
            "strategy": signal.strategy,
            "entry_price": signal.entry_price,
            "price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "weekly_confirmed": signal.weekly_confirmed,
            "confidence": signal.confidence,
            "daily_signal": signal_dict(signal.daily_signal),
        }
    )


def price_targets_dict(targets: PriceTargets) -> dict[str, Any]:
    """Serialize PriceTargets while retaining the useful nested zones."""

    return safe_value(
        {
            "ticker": targets.ticker,
            "current_price": targets.current_price,
            "buy_zone": targets.buy_zone,
            "sell_zone": targets.sell_zone,
            "fibonacci": targets.fibonacci,
            "support_resistance": targets.sr,
            "sr": targets.sr,
            "risk_reward": targets.risk_reward,
            "take_profits": targets.take_profits,
            "stop_loss": targets.stop_loss,
            "reasoning": targets.reasoning,
        }
    )


def portfolio_dict(tracker: PortfolioTracker) -> dict[str, Any]:
    """Serialize portfolio state and derived statistics."""

    positions = []
    for ticker, position in tracker.positions.items():
        item = safe_value(dataclasses.asdict(position))
        item["ticker"] = ticker
        item["market_value"] = safe_value(position.market_value)
        item["cost_basis"] = safe_value(position.cost_basis)
        item["pnl_since_peak"] = safe_value(position.pnl_since_peak)
        positions.append(item)

    stats = safe_value(tracker.stats())
    closed_trades = safe_value(tracker.closed_trades)
    return {
        "positions": positions,
        "closed_trades": closed_trades,
        "stats": stats,
        "sector_exposure": safe_value(tracker.sector_exposure()),
        "overexposed_sectors": safe_value(tracker.overexposed_sectors()),
        "storage_status": (
            "unavailable" if getattr(tracker, "storage_error", None) else "ok"
        ),
        "storage_recovered": bool(getattr(tracker, "recovered_corrupt_file", None)),
    }


def equity_curve_dict(curve: pd.Series) -> dict[str, Any]:
    values = [safe_value(value) for value in curve.tolist()]
    return {
        "points": [
            {"index": safe_value(index), "value": safe_value(value)}
            for index, value in enumerate(curve.tolist())
        ],
        "values": values,
    }


def backtest_dict(result: Any) -> dict[str, Any]:
    """Serialize a BacktestResult and its trade/equity details."""

    trades = []
    for trade in result.trades:
        trade_data = dataclasses.asdict(trade) if dataclasses.is_dataclass(trade) else vars(trade)
        item = safe_value(trade_data)
        item["is_open"] = bool(getattr(trade, "is_open", False))
        trades.append(item)
    return {
        "ticker": result.ticker,
        "strategy": result.strategy,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "initial_capital": safe_value(result.initial_capital),
        "final_capital": safe_value(result.final_capital),
        "total_trades": result.total_trades,
        "winning_trades": result.winning_trades,
        "losing_trades": result.losing_trades,
        "losing_rate": safe_value(result.losing_rate),
        "win_rate": safe_value(result.win_rate),
        "avg_win": safe_value(result.avg_win),
        "avg_loss": safe_value(result.avg_loss),
        "profit_factor": safe_value(result.profit_factor),
        "sharpe_ratio": safe_value(result.sharpe_ratio),
        "max_drawdown": safe_value(result.max_drawdown),
        "max_drawdown_pct": safe_value(result.max_drawdown_pct),
        "cagr": safe_value(result.cagr),
        "total_return_pct": safe_value(result.total_return_pct),
        "trades": trades,
        # The web client consumes an array of equity values.  ``equity_points``
        # retains index metadata for clients that need it.
        "equity_curve": [safe_value(value) for value in result.equity_curve.tolist()],
        "equity_points": equity_curve_dict(result.equity_curve)["points"],
        "summary": result.summary(),
    }


def dataframe_records(df: pd.DataFrame, limit: int | None = None) -> list[dict[str, Any]]:
    return safe_dataframe(df, limit=limit)


__all__ = [
    "backtest_dict",
    "confirmed_signal_dict",
    "dataframe_records",
    "equity_curve_dict",
    "portfolio_dict",
    "price_targets_dict",
    "safe_dataframe",
    "safe_value",
    "signal_dict",
]
